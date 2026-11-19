import json
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from proteus import Model
from trytond.exceptions import UserWarning
from trytond.modules.account.tests.tools import (
    create_chart, create_fiscalyear, get_accounts)
from trytond.modules.account_invoice.tests.tools import (
    set_fiscalyear_invoice_sequences)
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction


class TestEnableBankingSynchronization(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        key_patch = patch(
            'trytond.modules.account_statement_enable_banking.'
            'enable_banking.FERNET_KEY', Fernet.generate_key())
        self.addCleanup(key_patch.stop)
        key_patch.start()
        config = activate_modules(
            'account_statement_enable_banking', create_company, create_chart)
        company = get_company()
        fiscalyear = set_fiscalyear_invoice_sequences(create_fiscalyear())
        fiscalyear.click('create_period')
        revenue = get_accounts()['revenue']
        Party = Model.get('party.party')
        bank_party = Party(name='Test Bank')
        bank_party.save()
        Bank = Model.get('bank')
        bank = Bank(party=bank_party)
        bank.save()
        BankAccount = Model.get('bank.account')
        bank_account = BankAccount(bank=bank, currency=company.currency)
        bank_account.owners.append(company.party)
        bank_account.numbers.new(type='iban', number='ES9121000418450200051332')
        bank_account.save()
        Account = Model.get('account.account')
        cash, = Account.find([
            ('name', '=', 'Cash and Cash Equivalents')], limit=1)
        AccountJournal = Model.get('account.journal')
        account_journal, = AccountJournal.find([('code', '=', 'STA')], limit=1)
        Sequence = Model.get('ir.sequence')
        sequence, = Sequence.find([
            ('name', '=', 'Account Statement Origin')], limit=1)
        Journal = Model.get('account.statement.journal')
        journal = Journal(name='Enable Banking', journal=account_journal,
            account=cash, validation='balance', bank_account=bank_account,
            search_suggestions=False,
            account_statement_origin_sequence=sequence)
        journal.save()
        currency_code = company.currency.code

        with Transaction().start(config.database_name, config.user,
                context=config.context, _lock_tables=[
                    'account_statement_journal', 'ir_sequence',
                    'enable_banking_configuration']):
            Session = config.pool.get('enable_banking.session')
            Journal = config.pool.get('account.statement.journal')
            Origin = config.pool.get('account.statement.origin')
            Queue = config.pool.get('ir.queue')
            Statement = config.pool.get('account.statement')
            Configuration = config.pool.get('enable_banking.configuration')
            configuration = Configuration(1)
            configuration.date_field = 'transaction_date'
            configuration.offset = 2
            configuration.save()
            session_json = json.dumps({'accounts': [{
                'account_id': {'iban': 'ES9121000418450200051332'},
                'uid': 'external-account',
                }]})
            session, = Session.create([{
                'session_id': 'test-session',
                'bank': bank.id,
                'session': session_json,
                'valid_until': datetime.now() + timedelta(days=30),
                }])
            journal = Journal(journal.id)
            Journal.write([journal], {'enable_banking_session': session.id})
            today = date.today().isoformat()
            credit = {
                'entry_reference': 'credit-1',
                'transaction_amount': {
                    'amount': '100.25', 'currency': currency_code},
                'credit_debit_indicator': 'CRDT',
                'booking_date': today,
                'transaction_date': None,
                'value_date': today,
                'remittance_information': ['Invoice 123'],
                }
            debit = dict(credit, entry_reference='debit-1',
                credit_debit_indicator='DBIT',
                transaction_amount={
                    'amount': '20.10', 'currency': currency_code},
                debtor_account_additional_identification=[{
                    'identification': '0410',
                    'scheme_name': 'CPAN',
                    'issuer': 'MASTERCARD',
                    }, {
                    'identification': '1234',
                    'scheme_name': 'CPAN',
                    }],
                creditor_account_additional_identification=[
                    'legacy identifier', {'identification': '5678'}])
            responses = [
                Mock(status_code=200, json=Mock(return_value={
                    'transactions': [credit], 'continuation_key': 'page-2'})),
                Mock(status_code=200, json=Mock(return_value={
                    'transactions': [credit, debit]})),
                ]
            with patch(
                    'trytond.modules.account_statement_enable_banking.'
                    'journal.get_base_header', return_value={}), patch(
                    'trytond.modules.account_statement_enable_banking.'
                    'journal.requests.get', side_effect=responses) as request:
                journal._synchronize_statements_enable_banking()
                self.assertEqual(request.call_count, 2)
            origins = Origin.search([('statement.journal', '=', journal.id)])
            self.assertEqual(len(origins), 2)
            self.assertEqual(sorted(o.amount for o in origins),
                [Decimal('-20.10'), Decimal('100.25')])
            self.assertTrue(all(o.number and o.synchronized for o in origins))
            self.assertTrue(all(o.remittance_information == 'Invoice 123'
                for o in origins))
            debit_origin, = [o for o in origins
                if o.entry_reference == 'debit-1']
            self.assertEqual(debit_origin.information[
                    'debtor_account_additional_identification'],
                '0410 (CPAN, MASTERCARD); 1234 (CPAN)')
            self.assertEqual(debit_origin.information[
                    'creditor_account_additional_identification'],
                'legacy identifier; 5678')
            statement, = Statement.search([('journal', '=', journal.id)])
            self.assertEqual(statement.state, 'registered')
            self.assertEqual(statement.end_balance, Decimal('80.15'))

            with patch(
                    'trytond.modules.account_statement_enable_banking.'
                    'journal.get_base_header', return_value={}), patch(
                    'trytond.modules.account_statement_enable_banking.'
                    'journal.requests.get', return_value=responses[-1]):
                journal._synchronize_statements_enable_banking()
            self.assertEqual(Origin.search_count([
                ('statement.journal', '=', journal.id)]), 2)
            empty, = Statement.search([
                ('journal', '=', journal.id), ('id', '!=', statement.id)])
            self.assertEqual(empty.state, 'posted')
            self.assertEqual(empty.end_balance, Decimal('80.15'))

            # Changed references are imported for review, without suggestions.
            Journal.write([journal], {'search_suggestions': True})
            changed = dict(credit, entry_reference=today + '.4')
            changed_again = dict(credit, entry_reference=today + '.5')
            other_information = dict(credit, entry_reference='other-information',
                value_date=(date.today() - timedelta(days=1)).isoformat())
            other_description = dict(credit, entry_reference='other-description',
                remittance_information=['Invoice 456'])
            response = Mock(status_code=200, json=Mock(return_value={
                'transactions': [credit, changed, changed_again,
                    other_information, other_description]}))
            with patch(
                    'trytond.modules.account_statement_enable_banking.'
                    'journal.get_base_header', return_value={}), patch(
                    'trytond.modules.account_statement_enable_banking.'
                    'journal.requests.get', return_value=response):
                journal._synchronize_statements_enable_banking()
            queued_ids = {origin_id
                for task in Queue.search([])
                if task.data['model'] == 'account.statement.origin'
                and task.data['method'] == 'search_suggestions'
                for origin_id in task.data['instances']}
            origins = Origin.search([('statement.journal', '=', journal.id)])
            self.assertEqual(len(origins), 6)
            by_reference = {o.entry_reference: o for o in origins}
            duplicate = by_reference[changed['entry_reference']]
            second_duplicate = by_reference[changed_again['entry_reference']]
            original = by_reference['credit-1']
            self.assertEqual(queued_ids, {
                    by_reference['other-information'].id,
                    by_reference['other-description'].id})
            self.assertEqual(duplicate.review_state, 'discrepancy')
            self.assertIn('Entry Reference', duplicate.review_details)
            self.assertEqual({o.id for o in duplicate.review_origins},
                {original.id, second_duplicate.id})
            self.assertEqual({o.id for o in original.review_origins},
                {duplicate.id, second_duplicate.id})
            for reference in ('debit-1', 'other-information',
                    'other-description'):
                self.assertEqual(by_reference[reference].review_state, 'pending')
            duplicate_id = duplicate.id
            second_duplicate_id = second_duplicate.id
            original_id = original.id
            debit_id = by_reference['debit-1'].id
            Transaction().commit()

        # Review and posting use the same workflow as the client.
        Origin = Model.get('account.statement.origin')
        duplicate = Origin(duplicate_id)
        duplicate.click('search_suggestions')
        self.assertEqual(duplicate.state, 'registered')
        line = duplicate.lines.new()
        line.date = date.today()
        line.account = revenue
        line.amount = duplicate.amount
        duplicate.save()
        duplicate.click('post')
        self.assertEqual(duplicate.state, 'posted')
        self.assertEqual(duplicate.review_state, 'reviewed')
        self.assertEqual(duplicate.lines[0].move.state, 'posted')

        second_duplicate = Origin(second_duplicate_id)
        end_balance = second_duplicate.statement.end_balance
        with self.assertRaises(UserWarning) as warning:
            second_duplicate.click('mark_reviewed')
        self.assertIn('will cancel', warning.exception.message)
        second_duplicate.reload()
        self.assertEqual(second_duplicate.state, 'registered')
        self.assertEqual(second_duplicate.statement.end_balance, end_balance)
        Warning = Model.get('res.user.warning')
        User = Model.get('res.user')
        Warning(user=User(config.user), name=warning.exception.name).save()
        second_duplicate.click('mark_reviewed')
        self.assertEqual(second_duplicate.state, 'cancelled')
        self.assertEqual(second_duplicate.review_state, 'reviewed')
        self.assertEqual(second_duplicate.statement.end_balance,
            end_balance - second_duplicate.amount)
        original = Origin(original_id)
        self.assertEqual({o.id for o in original.review_origins},
            {duplicate_id, second_duplicate_id})

        # Statement posting no longer uses the old duplicate warning.
        line = original.lines.new()
        line.date = date.today()
        line.account = revenue
        line.amount = original.amount
        original.save()
        debit_origin = Origin(debit_id)
        line = debit_origin.lines.new()
        line.date = date.today()
        line.account = revenue
        line.amount = debit_origin.amount
        debit_origin.save()
        statement = original.statement
        statement.click('validate_statement')
        statement.click('post')
        self.assertEqual(statement.state, 'posted')
        original.reload()
        self.assertEqual(original.lines[0].move.state, 'posted')

        # Reused references with changed data must be kept, including within
        # one response, but downloading the same variants again is idempotent.
        with Transaction().start(config.database_name, config.user,
                context=config.context, _lock_tables=[
                    'account_statement_journal', 'ir_sequence',
                    'enable_banking_configuration']):
            Journal = config.pool.get('account.statement.journal')
            Origin = config.pool.get('account.statement.origin')
            journal = Journal(journal.id)
            Journal.write([journal], {'search_suggestions': False})
            changed_amount = dict(credit, transaction_amount={
                    'amount': '110.25', 'currency': currency_code})
            changed_date = dict(credit,
                booking_date=(date.today() - timedelta(days=1)).isoformat())
            changed_description = dict(credit,
                remittance_information=['Corrected invoice'])
            changed_information = dict(credit,
                value_date=(date.today() - timedelta(days=1)).isoformat())
            changed_balance = dict(credit,
                balance_after_transaction={'amount': '500.00'})
            response = Mock(status_code=200, json=Mock(return_value={
                    'transactions': [
                        credit, changed_amount, changed_date,
                        changed_description, changed_information,
                        changed_balance, changed_amount, changed_balance]}))
            with patch(
                    'trytond.modules.account_statement_enable_banking.'
                    'journal.get_base_header', return_value={}), patch(
                    'trytond.modules.account_statement_enable_banking.'
                    'journal.requests.get', return_value=response):
                journal._synchronize_statements_enable_banking()
                conflicts = Origin.search([
                        ('statement.journal', '=', journal.id),
                        ('entry_reference', '=', 'credit-1'),
                        ('id', '!=', original_id),
                        ], order=[('id', 'ASC')])
                self.assertEqual(len(conflicts), 4)
                self.assertEqual(conflicts[0].amount, Decimal('110.25'))
                self.assertEqual(conflicts[1].date,
                    date.today() - timedelta(days=1))
                self.assertEqual(conflicts[2].description, 'Corrected invoice')
                self.assertEqual(conflicts[3].information['value_date'],
                    changed_information['value_date'])
                # As in 7.8, a changed running balance alone is not a new
                # transaction variant.
                for conflict, label in zip(conflicts,
                        ['Amount', 'Date', 'Information', 'Information']):
                    self.assertEqual(conflict.review_state, 'discrepancy')
                    self.assertIn(original_id,
                        [o.id for o in conflict.review_origins])
                    self.assertIn(label, conflict.review_details)
                    self.assertEqual(conflict.state, 'registered')
                self.assertEqual(Origin(original_id).amount, Decimal('100.25'))
                self.assertEqual(Origin(original_id).statement.state, 'posted')
                self.assertEqual(
                    Origin(original_id).lines[0].move.state, 'posted')
                self.assertIn(conflicts[0].id,
                    [o.id for o in Origin(original_id).review_origins])
                journal._synchronize_statements_enable_banking()
                self.assertEqual(Origin.search_count([
                            ('statement.journal', '=', journal.id)]), 10)
            conflict_id = conflicts[0].id
            Transaction().commit()

        Origin = Model.get('account.statement.origin')
        conflict = Origin(conflict_id)
        line = conflict.lines.new()
        line.date = conflict.date
        line.account = revenue
        line.amount = conflict.amount
        conflict.save()
        conflict.click('post')
        self.assertEqual(conflict.state, 'posted')
        self.assertEqual(conflict.review_state, 'reviewed')
        self.assertEqual(conflict.lines[0].move.state, 'posted')
