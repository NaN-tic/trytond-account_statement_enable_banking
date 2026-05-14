# This file is part account_statement_enable_banking module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from unittest.mock import patch

from trytond.exceptions import UserError
from trytond.modules.account_statement_enable_banking.common import (
    load_session_json)
from trytond.pool import Pool
from trytond.tests.test_tryton import ModuleTestCase, with_transaction


class AccountStatementEnableBankingTestCase(ModuleTestCase):
    'Test Account Statement Enable Banking module'
    module = 'account_statement_enable_banking'
    extras = ['account_statement_aeb43', 'analytic_account']

    @with_transaction()
    def test_load_session_json_invalid_data(self):
        with self.assertRaises(UserError):
            load_session_json(None)

    @with_transaction()
    def test_set_ebsession_updates_all_matching_journals(self):
        Journal = Pool().get('account.statement.journal')

        class DummySession:
            def __init__(self, session_id, allowed_bank_accounts, encrypted_session,
                    aspsp_name='Bank', aspsp_country='ES'):
                self.id = session_id
                self.allowed_bank_accounts = allowed_bank_accounts
                self.encrypted_session = encrypted_session
                self.aspsp_name = aspsp_name
                self.aspsp_country = aspsp_country

        class DummyJournal:
            def __init__(self, bank_account, enable_banking_session=None,
                    aspsp_name=None, aspsp_country=None):
                self.bank_account = bank_account
                self.enable_banking_session = enable_banking_session
                self.aspsp_name = aspsp_name
                self.aspsp_country = aspsp_country

            def on_change_enable_banking_session(self):
                Journal.on_change_enable_banking_session(self)

        old_session = DummySession(
            2, [], b'old', aspsp_name='Old', aspsp_country='FR')
        new_session = DummySession(1, ['acc-1', 'acc-2'], b'new')
        journal_1 = DummyJournal(
            'acc-1', enable_banking_session=old_session,
            aspsp_name='Old', aspsp_country='FR')
        journal_2 = DummyJournal('acc-2')
        journal_3 = DummyJournal(
            'acc-3', enable_banking_session=old_session,
            aspsp_name='Old', aspsp_country='FR')

        search_calls = []

        class DummyEBSession:
            @staticmethod
            def browse(ids):
                return ids

            @staticmethod
            def delete(sessions):
                return None

        def search(domain):
            search_calls.append(domain)
            if ('bank_account', 'in', new_session.allowed_bank_accounts) in domain:
                return [journal_1, journal_2]
            if domain == [('enable_banking_session', 'in', [old_session.id])]:
                return []
            self.fail(f'Unexpected search domain: {domain}')

        with patch.object(Journal, 'search', side_effect=search), \
                patch.object(Journal, 'save') as save, \
                patch('trytond.modules.account_statement_enable_banking.journal.Pool.get',
                    return_value=DummyEBSession) as pool_get, \
                patch.object(DummyEBSession, 'delete') as delete:
            Journal.set_ebsession(new_session)

        self.assertIs(journal_1.enable_banking_session, new_session)
        self.assertIs(journal_2.enable_banking_session, new_session)
        self.assertIs(journal_3.enable_banking_session, old_session)
        self.assertEqual(journal_1.aspsp_name, 'Old')
        self.assertEqual(journal_1.aspsp_country, 'FR')
        self.assertEqual(journal_2.aspsp_name, 'Bank')
        self.assertEqual(journal_2.aspsp_country, 'ES')
        self.assertEqual(len(search_calls), 2)
        self.assertIn(
            ('bank_account', 'in', new_session.allowed_bank_accounts),
            search_calls[0])
        self.assertEqual(
            search_calls[1],
            [('enable_banking_session', 'in', [old_session.id])])
        pool_get.assert_called_once_with('enable_banking.session')
        save.assert_called_once_with([journal_1, journal_2])
        delete.assert_called_once_with([old_session.id])

    @with_transaction()
    def test_set_ebsession_keeps_shared_old_session(self):
        Journal = Pool().get('account.statement.journal')

        class DummySession:
            def __init__(self, session_id, allowed_bank_accounts, encrypted_session,
                    aspsp_name='Bank', aspsp_country='ES'):
                self.id = session_id
                self.allowed_bank_accounts = allowed_bank_accounts
                self.encrypted_session = encrypted_session
                self.aspsp_name = aspsp_name
                self.aspsp_country = aspsp_country

        class DummyJournal:
            def __init__(self, bank_account, enable_banking_session=None,
                    aspsp_name=None, aspsp_country=None):
                self.bank_account = bank_account
                self.enable_banking_session = enable_banking_session
                self.aspsp_name = aspsp_name
                self.aspsp_country = aspsp_country

            def on_change_enable_banking_session(self):
                Journal.on_change_enable_banking_session(self)

        old_session = DummySession(
            2, [], b'old', aspsp_name='Old', aspsp_country='FR')
        new_session = DummySession(1, ['acc-1'], b'new')
        journal_1 = DummyJournal(
            'acc-1', enable_banking_session=old_session,
            aspsp_name='Old', aspsp_country='FR')
        shared_journal = DummyJournal(
            'acc-shared', enable_banking_session=old_session,
            aspsp_name='Old', aspsp_country='FR')

        search_calls = []

        class DummyEBSession:
            @staticmethod
            def browse(ids):
                return ids

            @staticmethod
            def delete(sessions):
                return None

        def search(domain):
            search_calls.append(domain)
            if ('bank_account', 'in', new_session.allowed_bank_accounts) in domain:
                return [journal_1]
            if domain == [('enable_banking_session', 'in', [old_session.id])]:
                return [shared_journal]
            self.fail(f'Unexpected search domain: {domain}')

        with patch.object(Journal, 'search', side_effect=search), \
                patch.object(Journal, 'save') as save, \
                patch('trytond.modules.account_statement_enable_banking.journal.Pool.get',
                    return_value=DummyEBSession) as pool_get, \
                patch.object(DummyEBSession, 'delete') as delete:
            Journal.set_ebsession(new_session)

        self.assertIs(journal_1.enable_banking_session, new_session)
        self.assertIs(shared_journal.enable_banking_session, old_session)
        self.assertEqual(journal_1.aspsp_name, 'Old')
        self.assertEqual(journal_1.aspsp_country, 'FR')
        self.assertEqual(len(search_calls), 2)
        self.assertIn(
            ('bank_account', 'in', new_session.allowed_bank_accounts),
            search_calls[0])
        self.assertEqual(
            search_calls[1],
            [('enable_banking_session', 'in', [old_session.id])])
        pool_get.assert_called_once_with('enable_banking.session')
        save.assert_called_once_with([journal_1])
        delete.assert_not_called()

del ModuleTestCase
