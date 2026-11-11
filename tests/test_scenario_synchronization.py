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
                    'amount': '20.10', 'currency': currency_code})
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

            # Changed bank references must not suppress downloads or suggestions.
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
                    duplicate.id, second_duplicate.id,
                    by_reference['other-information'].id,
                    by_reference['other-description'].id})
            self.assertTrue(duplicate.possible_duplicate)
            self.assertEqual({o.id for o in duplicate.possible_duplicates},
                {original.id, second_duplicate.id})
            self.assertEqual({o.id for o in original.possible_duplicates},
                {duplicate.id, second_duplicate.id})
            for reference in ('debit-1', 'other-information',
                    'other-description'):
                self.assertFalse(by_reference[reference].possible_duplicate)
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
        with self.assertRaises(UserWarning) as warning:
            duplicate.click('post')
        self.assertIn('may duplicate', warning.exception.message)
        duplicate.reload()
        self.assertEqual(duplicate.state, 'registered')
        self.assertFalse(duplicate.lines[0].move)

        duplicate.duplicates_reviewed = True
        duplicate.click('post')
        self.assertEqual(duplicate.state, 'posted')
        self.assertEqual(duplicate.lines[0].move.state, 'posted')

        second_duplicate = Origin(second_duplicate_id)
        second_duplicate.click('cancel')
        original = Origin(original_id)
        self.assertEqual({o.id for o in original.possible_duplicates},
            {duplicate_id})

        # Posting a validated statement must also warn, and allow confirmation.
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
        with self.assertRaises(UserWarning) as warning:
            statement.click('post')
        statement.reload()
        self.assertEqual(statement.state, 'validated')
        Warning = Model.get('res.user.warning')
        User = Model.get('res.user')
        Warning(user=User(config.user), name=warning.exception.name).save()
        statement.click('post')
        self.assertEqual(statement.state, 'posted')
        original.reload()
        self.assertEqual(original.lines[0].move.state, 'posted')
