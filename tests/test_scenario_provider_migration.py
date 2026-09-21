import unittest

from proteus import Model
from trytond.modules.account.tests.tools import create_chart
from trytond.modules.company.tests.tools import create_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction


class TestProviderMigration(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        config = activate_modules(
            'account_statement_enable_banking', create_company, create_chart)
        Account = Model.get('account.account')
        cash, = Account.find([
            ('name', '=', 'Cash and Cash Equivalents')], limit=1)
        AccountJournal = Model.get('account.journal')
        account_journal, = AccountJournal.find([('code', '=', 'STA')], limit=1)
        Sequence = Model.get('ir.sequence')
        sequence, = Sequence.find([
            ('name', '=', 'Account Statement Origin')], limit=1)
        Journal = Model.get('account.statement.journal')
        automatic = Journal(name='Existing Automatic Journal', account=cash,
            journal=account_journal, validation='balance',
            account_statement_origin_sequence=sequence, synchronize_journal=True)
        automatic.save()
        manual = Journal(name='Existing Manual Journal', account=cash,
            journal=account_journal, validation='balance', statement_provider='',
            account_statement_origin_sequence=sequence)
        manual.save()

        with Transaction().start(config.database_name, 0) as transaction:
            Journal = config.pool.get('account.statement.journal')
            table = Journal.__table__()
            handler = Journal.__table_handler__('account_statement_common')
            handler.drop_column('statement_provider')
            Journal.__register__('account_statement_common')
            cursor = transaction.connection.cursor()
            cursor.execute(*table.select(table.id, table.statement_provider))
            providers = dict(cursor.fetchall())
            self.assertEqual(providers[automatic.id], 'enable_banking')
            self.assertEqual(providers[manual.id], '')
            # A later user choice of manual import must survive another upgrade.
            cursor.execute(*table.update([table.statement_provider], [''],
                where=table.id == automatic.id))
            Journal.__register__('account_statement_common')
            cursor.execute(*table.select(table.statement_provider,
                where=table.id == automatic.id))
            self.assertEqual(cursor.fetchone(), ('',))
