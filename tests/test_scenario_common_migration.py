import unittest
from decimal import Decimal

from proteus import Model, Wizard
from sql import Table
from trytond.modules.account.tests.tools import create_chart
from trytond.modules.company.tests.tools import create_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.tools import file_open
from trytond.transaction import Transaction


class TestCommonMigration(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        config = activate_modules([
            'account_statement_enable_banking', 'analytic_account',
            'account_statement_aeb43'], create_company, create_chart)
        Account = Model.get('account.account')
        cash, = Account.find([
            ('name', '=', 'Cash and Cash Equivalents')], limit=1)
        AccountJournal = Model.get('account.journal')
        account_journal, = AccountJournal.find([('code', '=', 'STA')], limit=1)
        Sequence = Model.get('ir.sequence')
        sequence, = Sequence.find([
            ('name', '=', 'Account Statement Origin')], limit=1)
        sequence.prefix = 'CUSTOM-'
        sequence.number_next = 123
        sequence.save()
        Journal = Model.get('account.statement.journal')
        journal = Journal(name='Existing Journal', journal=account_journal,
            account=cash, validation='balance',
            account_statement_origin_sequence=sequence)
        journal.weights.new(type='party-match', weight=35)
        journal.save()
        Statement = Model.get('account.statement')
        statement = Statement(name='Existing Statement', journal=journal,
            start_balance=Decimal(0), end_balance=Decimal(10))
        origin = statement.origins.new()
        origin.date = statement.date
        origin.amount = Decimal(10)
        statement.save()
        statement.click('register')
        origin, = statement.origins
        origin_id = origin.id

        old_module = 'account_statement_enable_banking'
        new_module = 'account_statement_common'
        with Transaction().start(config.database_name, 0) as transaction:
            pool = config.pool
            Data = pool.get('ir.model.data')
            Session = pool.get('enable_banking.session')
            Translation = pool.get('ir.translation')
            for language in ('ca', 'es'):
                for module_name in (new_module, old_module):
                    with file_open(
                            f'{module_name}/locale/{language}.po') as source:
                        Translation.translation_import(
                            language, module_name, source.read())
            translation_ids = {translation.id for translation in
                Translation.search([
                    ('module', '=', new_module),
                    ('lang', 'in', ['ca', 'es']),
                    ])}
            session, = Session.create([{
                'session_id': 'preserved-session',
                'encrypted_session': b'preserved-ciphertext',
                }])
            shared_ids = {data.fs_id: (data.model, data.db_id)
                for data in Data.search([('module', '=', new_module)])}
            connector_ids = {data.fs_id: (data.model, data.db_id)
                for data in Data.search([('module', '=', old_module)])}

            # Reconstruct pre-split ownership and module state while retaining
            # the actual accounting records and customized sequence.
            cursor = transaction.connection.cursor()
            for table_name in ('ir_model_data', 'ir_model', 'ir_model_field',
                    'ir_ui_view', 'ir_translation'):
                table = Table(table_name)
                cursor.execute(*table.update([table.module], [old_module],
                    where=table.module == new_module))
            module = Table('ir_module')
            cursor.execute(*module.update([module.state], ['not activated'],
                where=module.name == new_module))
            Data._get_id_cache.clear()

        Module = Model.get('ir.module')
        common, = Module.find([('name', '=', new_module)])
        connector, = Module.find([('name', '=', old_module)])
        common.click('activate')
        connector.click('upgrade')
        Wizard('ir.module.activate_upgrade').execute('upgrade')

        with Transaction().start(config.database_name, 0):
            Data = config.pool.get('ir.model.data')
            Session = config.pool.get('enable_banking.session')
            View = config.pool.get('ir.ui.view')
            Translation = config.pool.get('ir.translation')
            self.assertEqual(translation_ids, {translation.id for translation in
                Translation.search([
                    ('module', '=', new_module),
                    ('lang', 'in', ['ca', 'es']),
                    ])})
            self.assertEqual(shared_ids, {
                data.fs_id: (data.model, data.db_id)
                for data in Data.search([('module', '=', new_module)])})
            self.assertEqual(connector_ids, {
                data.fs_id: (data.model, data.db_id)
                for data in Data.search([('module', '=', old_module)])})
            for model, db_id in shared_ids.values():
                if model == 'ir.ui.view':
                    self.assertEqual(View(db_id).module, new_module)
            self.assertEqual(Session(session.id).encrypted_session,
                b'preserved-ciphertext')
            # Re-running the migration must preserve the same references.
            Data.migrate_enable_banking_data()
            self.assertEqual(Data.get_id(new_module,
                'sequence_account_statement_origin'), sequence.id)

        Journal = Model.get('account.statement.journal')
        journal = Journal(journal.id)
        self.assertEqual(journal.weights[0].weight, 35)
        self.assertEqual(journal.account_statement_origin_sequence.id,
            sequence.id)
        Sequence = Model.get('ir.sequence')
        sequence = Sequence(sequence.id)
        self.assertEqual(sequence.prefix, 'CUSTOM-')
        self.assertEqual(sequence.number_next, 123)
        Origin = Model.get('account.statement.origin')
        origin = Origin(origin_id)
        self.assertEqual(origin.amount, Decimal(10))
        self.assertEqual(origin.state, 'registered')
        create_lines = Wizard('account.statement.origin.create_line',
            models=[origin])
        self.assertIsNotNone(create_lines.form)
        create_lines.execute('end')
