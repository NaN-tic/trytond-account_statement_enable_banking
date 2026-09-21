import json
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from proteus import Model
from trytond.modules.account.tests.tools import create_chart
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
        config = activate_modules(
            'account_statement_enable_banking', create_company, create_chart)
        company = get_company()
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
            Statement = config.pool.get('account.statement')
            Configuration = config.pool.get('enable_banking.configuration')
            configuration = Configuration(1)
            configuration.date_field = 'transaction_date'
            configuration.offset = 2
            configuration.save()
            session, = Session.create([{
                'session_id': 'test-session',
                'bank': bank.id,
                'encrypted_session': b'encrypted-session',
                'valid_until': datetime.now() + timedelta(days=30),
                }])
            journal = Journal(journal.id)
            session_json = json.dumps({'accounts': [{
                'account_id': {'iban': 'ES9121000418450200051332'},
                'uid': 'external-account',
                }]})
            with patch.object(Session, '_get_session',
                    return_value=session_json):
                Journal.write([journal], {'enable_banking_session': session.id})
            today = date.today().isoformat()
            credit = {
                'entry_reference': 'credit-1',
                'transaction_amount': {
                    'amount': '100.25', 'currency': currency_code},
                'credit_debit_indicator': 'CRDT',
                'transaction_date': today,
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
            with patch.object(Session, '_get_session',
                    return_value=session_json), patch(
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

            with patch.object(Session, '_get_session',
                    return_value=session_json), patch(
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
