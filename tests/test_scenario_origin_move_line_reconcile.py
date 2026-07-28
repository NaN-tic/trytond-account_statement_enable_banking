import datetime as dt
import unittest
from decimal import Decimal

from proteus import Model
from trytond.modules.account.tests.tools import (
    create_chart, create_fiscalyear, get_accounts)
from trytond.modules.account_invoice.tests.tools import (
    create_payment_term, set_fiscalyear_invoice_sequences)
from trytond.modules.company.tests.tools import create_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules


class Test(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        today = dt.date.today()

        activate_modules(
            ['account_statement_enable_banking', 'account_invoice'],
            create_company, create_chart)

        fiscalyear = set_fiscalyear_invoice_sequences(
            create_fiscalyear(today=today))
        fiscalyear.click('create_period')

        Account = Model.get('account.account')
        accounts = get_accounts()
        receivable = accounts['receivable']
        revenue = accounts['revenue']
        cash, = Account.find([
                ('code', '=', '1.1.1000'),
                ], limit=1)

        Party = Model.get('party.party')
        customer = Party(name='Customer')
        customer.save()

        payment_term = create_payment_term()
        payment_term.save()

        Invoice = Model.get('account.invoice')
        invoice = Invoice(type='out')
        invoice.party = customer
        invoice.payment_term = payment_term
        invoice_line = invoice.lines.new()
        invoice_line.quantity = 1
        invoice_line.unit_price = Decimal('142.50')
        invoice_line.account = revenue
        invoice_line.description = 'Test'
        invoice.click('post')
        self.assertEqual(invoice.state, 'posted')

        move_line, = [l for l in invoice.lines_to_pay if l.reconciliation is None]

        StatementJournal = Model.get('account.statement.journal')
        Statement = Model.get('account.statement')
        AccountJournal = Model.get('account.journal')
        Sequence = Model.get('ir.sequence')
        account_statement_origin_sequence, = Sequence.find([
            ('name', '=', 'Account Statement Origin'),
            ], limit=1)
        account_journal, = AccountJournal.find([('code', '=', 'STA')], limit=1)
        statement_journal = StatementJournal(
            name='Test',
            journal=account_journal,
            account=cash,
            validation='number_of_lines',
            account_statement_origin_sequence=account_statement_origin_sequence,
            )
        statement_journal.save()

        statement = Statement(name='move line origin')
        statement.journal = statement_journal
        statement.number_of_lines = 2

        origin = statement.origins.new()
        origin.date = today
        origin.amount = Decimal('142.50')
        origin.party = customer

        pending_origin = statement.origins.new()
        pending_origin.date = today
        pending_origin.amount = Decimal('1.00')
        pending_origin.party = customer

        statement.click('validate_statement')

        origin, pending_origin = statement.origins
        line = origin.lines.new()
        line.date = today
        line.party = customer
        line.account = receivable
        line.related_to = move_line

        self.assertEqual(line.amount, Decimal('142.50'))

        origin.click('post')

        origin.reload()
        pending_origin.reload()
        statement.reload()
        invoice.reload()
        move_line.reload()

        self.assertEqual(origin.state, 'posted')
        self.assertEqual(pending_origin.state, 'registered')
        self.assertEqual(statement.state, 'validated')
        self.assertEqual(origin.lines[0].related_to.id, move_line.id)
        self.assertIsNotNone(move_line.reconciliation)
        self.assertEqual(invoice.state, 'paid')
