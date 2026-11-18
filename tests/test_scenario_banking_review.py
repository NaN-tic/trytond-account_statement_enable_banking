import json
import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from proteus import Model
from trytond.model.exceptions import AccessError
from trytond.modules.account.tests.tools import (
    create_chart, create_fiscalyear, get_accounts)
from trytond.modules.account_invoice.tests.tools import (
    set_fiscalyear_invoice_sequences)
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules


class TestBankingReview(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        config = activate_modules('account_statement_enable_banking',
            create_company, create_chart)
        company = get_company()
        today = date.today()
        booking_date = today - timedelta(days=3)
        fiscalyear = set_fiscalyear_invoice_sequences(
            create_fiscalyear(today=booking_date))
        fiscalyear.click('create_period')
        Account = Model.get('account.account')
        cash, = Account.find([
                ('name', '=', 'Cash and Cash Equivalents')], limit=1)
        accounts = get_accounts()
        AccountJournal = Model.get('account.journal')
        account_journal, = AccountJournal.find([('code', '=', 'STA')], limit=1)
        Sequence = Model.get('ir.sequence')
        sequence, = Sequence.find([
                ('name', '=', 'Account Statement Origin')], limit=1)
        BankAccount = Model.get('bank.account')
        bank_account = BankAccount()
        bank_account.owners.append(company.party)
        bank_account.numbers.new(
            type='iban', number='ES9121000418450200051332')
        bank_account.save()
        Session = Model.get('enable_banking.session')
        session = Session(encrypted_session=b'test')
        session.save()
        Journal = Model.get('account.statement.journal')
        journal = Journal(name='Review Bank', journal=account_journal,
            account=cash, bank_account=bank_account,
            account_statement_origin_sequence=sequence,
            search_suggestions=False, validation='balance',
            review_start_date=booking_date - timedelta(days=2))
        session_data = json.dumps({'accounts': [{
                    'uid': 'test-account',
                    'account_id': {'iban': 'ES9121000418450200051332'},
                    }]})
        session_model = config.pool.get('enable_banking.session')
        with patch.object(session_model, '_get_session',
                return_value=session_data):
            journal.enable_banking_session = session
            journal.save()
        self.assertEqual(journal.review_margin_days, 3)
        Origin = Model.get('account.statement.origin')
        Configuration = Model.get('enable_banking.configuration')
        configuration = Configuration(1)
        configuration.date_field = 'transaction_date'
        configuration.offset = 10
        configuration.save()

        def transaction(reference, amount, text):
            return {
                'entry_reference': reference,
                'transaction_date': booking_date.isoformat(),
                'transaction_amount': {
                    'currency': company.currency.code, 'amount': str(amount)},
                'credit_debit_indicator': 'CRDT',
                'remittance_information': [text],
                }

        original = transaction('stable', 10, 'Stable payment')
        changed = transaction('old-reference', 20, 'Changed reference')
        another_candidate = transaction(
            'another-reference', 20, 'Changed reference')
        changed_amount = transaction('same-reference', 30, 'Changed amount')
        missing = transaction('missing', 40, 'Missing payment')

        def download(transactions, review=False, responses=None):
            response = Mock(status_code=200)
            response.json.return_value = {'transactions': transactions}
            with patch.object(session_model, '_get_session',
                    return_value=session_data), patch(
                    'trytond.modules.account_statement_enable_banking.'
                    'journal.get_base_header', return_value={}), patch(
                    'trytond.modules.account_statement_enable_banking.'
                    'journal.requests.get', side_effect=responses,
                    return_value=response) as request:
                journal.click('review_statement_enable_banking' if review
                    else 'synchronize_statement_enable_banking')
                return request.call_args.kwargs['params']

        download([original, changed, changed_amount, missing])
        origins = Origin.find([('journal', '=', journal.id)])
        self.assertEqual(len(origins), 4)
        self.assertEqual({o.review_state for o in origins}, {'pending'})
        # The next ordinary download detects the new reference immediately.
        download([original, another_candidate, changed_amount, missing])
        conflicts = Origin.find([
                ('journal', '=', journal.id),
                ('review_state', '=', 'discrepancy')])
        self.assertEqual(len(conflicts), 2)
        self.assertEqual({o.entry_reference for o in conflicts},
            {'old-reference', 'another-reference'})
        self.assertTrue(all(len(o.review_origins) == 1 for o in conflicts))
        stable, = [o for o in origins if o.entry_reference == 'stable']
        line = stable.lines.new()
        line.account = accounts['revenue']
        line.amount = stable.amount
        stable.save()
        stable.click('post')
        self.assertEqual(stable.review_state, 'pending')
        download([original, changed, changed_amount, missing])
        self.assertEqual(len(Origin.find([('journal', '=', journal.id)])), 5)

        # A Norma 43 import has a description but no bank information.
        Statement = Model.get('account.statement')
        manual_statement = Statement(
            name='Norma 43', journal=journal,
            start_balance=Decimal(0), end_balance=Decimal(70))
        manual_origin = manual_statement.origins.new()
        manual_origin.date = booking_date
        manual_origin.amount = Decimal(70)
        manual_origin.description = 'Norma 43 payment'
        manual_statement.save()
        manual_statement.click('register')

        revised = transaction('new-reference', 20, 'Changed reference')
        revised_amount = transaction('same-reference', 35, 'Changed amount')
        new = transaction('new', 50, 'Late payment')
        norma = transaction('norma-reference', 70, 'Norma 43 payment')
        verified_transactions = [original, revised, revised_amount, new, norma]
        download(verified_transactions)
        self.assertEqual(len(Origin.find([
                    ('journal', '=', journal.id),
                    ('review_state', '=', 'discrepancy')])), 7)
        params = download(verified_transactions, review=True)
        self.assertEqual(params['date_to'], booking_date.isoformat())
        journal.reload()
        self.assertEqual(journal.reviewed_until, booking_date)

        stable.reload()
        self.assertEqual(stable.review_state, 'reviewed')
        origins = Origin.find([('journal', '=', journal.id)])
        self.assertEqual(len(origins), 10)
        self.assertEqual(len([o for o in origins
                    if o.review_state == 'discrepancy']), 8)
        revised_origin, = [o for o in origins
            if o.entry_reference == 'new-reference']
        self.assertEqual(len(revised_origin.review_origins), 2)
        self.assertIn('Entry Reference', revised_origin.review_details)
        revised_origin.click('mark_reviewed')
        self.assertEqual(revised_origin.state, 'cancelled')
        self.assertEqual(revised_origin.review_state, 'reviewed')
        amount_origin, = [o for o in origins
            if o.entry_reference == 'same-reference' and o.amount == 35]
        line = amount_origin.lines.new()
        line.account = accounts['revenue']
        line.amount = amount_origin.amount
        amount_origin.save()
        amount_origin.click('post')
        self.assertEqual(amount_origin.review_state, 'reviewed')
        download(verified_transactions, review=True)
        self.assertEqual(len(Origin.find([('journal', '=', journal.id)])), 10)
        revised_origin.reload()
        self.assertEqual(revised_origin.review_state, 'reviewed')
        missing_origin, = [o for o in origins if o.entry_reference == 'missing']
        self.assertIn('not returned', missing_origin.review_details)
        missing_origin.click('mark_reviewed')
        self.assertEqual(missing_origin.state, 'registered')

        first_page = Mock(status_code=200)
        first_page.json.return_value = {
            'transactions': [transaction('incomplete', 60, 'Incomplete')],
            'continuation_key': 'next'}
        failed_page = Mock(status_code=400)
        with self.assertRaises(AccessError):
            download([], review=True, responses=[first_page, failed_page])
        self.assertEqual(len(Origin.find([('journal', '=', journal.id)])), 10)
        journal.reload()
        self.assertEqual(journal.reviewed_until, booking_date)

        # A genuinely new variant reopens previously resolved conflicts.
        download([transaction('third-reference', 20, 'Changed reference')])
        revised_origin.reload()
        self.assertEqual(revised_origin.review_state, 'discrepancy')
        third, = Origin.find([('entry_reference', '=', 'third-reference')])
        self.assertEqual(third.review_state, 'discrepancy')
        self.assertEqual(len(third.review_origins), 3)

        # An exact bank match must not hide an unlinked partial match.
        duplicate_statement = Statement(name='Existing duplicate',
            journal=journal, start_balance=Decimal(0), end_balance=Decimal(10))
        duplicate = duplicate_statement.origins.new()
        duplicate.date = booking_date
        duplicate.amount = Decimal(10)
        duplicate.description = 'Stable payment'
        duplicate_statement.save()
        duplicate_statement.click('register')
        duplicate, = duplicate_statement.origins
        download(verified_transactions, review=True)
        stable.reload()
        duplicate.reload()
        self.assertEqual(stable.review_state, 'discrepancy')
        self.assertEqual(duplicate.review_state, 'discrepancy')
        self.assertIn(duplicate, stable.review_origins)
        self.assertEqual(len(Origin.find([('journal', '=', journal.id)])), 12)
