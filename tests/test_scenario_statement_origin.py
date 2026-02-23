import datetime as dt
import unittest
from decimal import Decimal

from proteus import Model, Wizard
# from trytond.exceptions import StatementPostError
from trytond.modules.account_statement.exceptions import StatementPostError
from trytond.modules.account.tests.tools import (
    create_chart, create_fiscalyear, get_accounts)
from trytond.modules.account_invoice.tests.tools import (
    set_fiscalyear_invoice_sequences)
from trytond.modules.company.tests.tools import create_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules, assertEqual


class Test(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):

        today = dt.date.today()

        # Activate modules
        activate_modules('account_statement_enable_banking', create_company, create_chart)

        # Create fiscal year
        fiscalyear = set_fiscalyear_invoice_sequences(
        create_fiscalyear(today=today))
        fiscalyear.click('create_period')

        # Get accounts
        Account = Model.get('account.account')
        accounts = get_accounts()
        receivable = accounts['receivable']
        expense = accounts['expense']
        cash, = Account.find([
                ('code', '=', '1.1.1000'), # Main Cash
                ], limit=1)

        # Create parties
        Party = Model.get('party.party')
        customer = Party(name="Customer")
        customer.save()

        # Create a statement with origins
        AccountJournal = Model.get('account.journal')
        StatementJournal = Model.get('account.statement.journal')
        Statement = Model.get('account.statement')
        Sequence = Model.get('ir.sequence')
        account_statement_origin_sequence, = Sequence.find([
            ('name', '=', 'Account Statement Origin'),
            ], limit=1)
        account_journal, = AccountJournal.find([('code', '=', 'STA')], limit=1)
        journal_number = StatementJournal(
            name="Number",
            journal=account_journal,
            account=cash,
            validation='number_of_lines',
            account_statement_origin_sequence=account_statement_origin_sequence,
            )
        journal_number.save()
        statement = Statement(name="number origins")
        statement.journal = journal_number
        statement.number_of_lines = 1
        origin = statement.origins.new()
        origin.date = today
        origin.amount = Decimal('50.00')
        origin.party = customer
        statement.click('validate_statement')

        # Statement can not be posted until all origins are finished
        with self.assertRaises(StatementPostError):
            statement.click('post')
        statement.click('draft')

        self.assertEqual(len(origin.lines), 0)

        origin, = statement.origins
        create_lines = Wizard('account.statement.origin.create_line',
            models=[origin])
        create_lines.form.account = receivable
        create_lines.form.party = customer
        create_lines.form.description = 'Auto'
        create_lines.execute('create_lines')

        origin.reload()
        self.assertEqual(len(origin.lines), 1)
        line, = origin.lines

        assertEqual(line.date, today)
        self.assertEqual(line.amount, Decimal('50.00'))
        assertEqual(line.party, customer)
        assertEqual(line.account, receivable)
        self.assertEqual(line.description, 'Auto')

        line.amount = Decimal('52.00')
        line = origin.lines.new()
        self.assertEqual(line.amount, Decimal('-2.00'))
        line.account = expense
        line.description = "Bank Fees"
        statement.click('post')
        self.assertEqual(statement.state, 'posted')
