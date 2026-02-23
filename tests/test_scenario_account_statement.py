import datetime as dt
import unittest
from decimal import Decimal

from proteus import Model
from trytond.modules.account_statement.exceptions import StatementValidateError
from trytond.modules.account.tests.tools import (
    create_chart, create_fiscalyear, get_accounts)
from trytond.modules.account_invoice.tests.tools import (
    create_payment_term, set_fiscalyear_invoice_sequences)
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
        activate_modules(['account_statement_enable_banking', 'account_invoice'],
            create_company, create_chart)

        # Create fiscal year
        fiscalyear = set_fiscalyear_invoice_sequences(
        create_fiscalyear(today=today))
        fiscalyear.click('create_period')

        # Get accounts
        Account = Model.get('account.account')
        accounts = get_accounts()
        receivable = accounts['receivable']
        payable = accounts['payable']
        revenue = accounts['revenue']
        expense = accounts['expense']
        cash, = Account.find([
                ('code', '=', '1.1.1000'), # Main Cash
                ], limit=1)

        # Create parties
        Party = Model.get('party.party')
        supplier = Party(name='Supplier')
        supplier.save()
        customer = Party(name='Customer')
        customer.save()

        # Create payment term
        payment_term = create_payment_term()
        payment_term.save()

        # Create 2 customer invoices
        Invoice = Model.get('account.invoice')
        customer_invoice1 = Invoice(type='out')
        customer_invoice1.party = customer
        customer_invoice1.payment_term = payment_term
        invoice_line = customer_invoice1.lines.new()
        invoice_line.quantity = 1
        invoice_line.unit_price = Decimal('100')
        invoice_line.account = revenue
        invoice_line.description = 'Test'
        customer_invoice1.click('post')
        self.assertEqual(customer_invoice1.state, 'posted')
        customer_invoice2 = Invoice(type='out')
        customer_invoice2.party = customer
        customer_invoice2.payment_term = payment_term
        invoice_line = customer_invoice2.lines.new()
        invoice_line.quantity = 1
        invoice_line.unit_price = Decimal('150')
        invoice_line.account = revenue
        invoice_line.description = 'Test'
        customer_invoice2.click('post')
        self.assertEqual(customer_invoice2.state, 'posted')

        # Create 1 customer credit note
        customer_credit_note = Invoice(type='out')
        customer_credit_note.party = customer
        customer_credit_note.payment_term = payment_term
        invoice_line = customer_credit_note.lines.new()
        invoice_line.quantity = -1
        invoice_line.unit_price = Decimal('50')
        invoice_line.account = revenue
        invoice_line.description = 'Test'
        customer_credit_note.click('post')
        self.assertEqual(customer_credit_note.state, 'posted')

        # Create 1 supplier invoices
        supplier_invoice = Invoice(type='in')
        supplier_invoice.party = supplier
        supplier_invoice.payment_term = payment_term
        invoice_line = supplier_invoice.lines.new()
        invoice_line.quantity = 1
        invoice_line.unit_price = Decimal('50')
        invoice_line.account = expense
        invoice_line.description = 'Test'
        supplier_invoice.invoice_date = today
        supplier_invoice.click('post')
        self.assertEqual(supplier_invoice.state, 'posted')

        # Create statement
        StatementJournal = Model.get('account.statement.journal')
        Statement = Model.get('account.statement')
        StatementLine = Model.get('account.statement.line')
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
            validation='balance',
            account_statement_origin_sequence=account_statement_origin_sequence,
            )
        statement_journal.save()

        statement = Statement(
            name='test',
            journal=statement_journal,
            start_balance=Decimal('0'),
            end_balance=Decimal('80'),
            )

        # Received 180 from customer
        statement_line = StatementLine()
        statement.lines.append(statement_line)
        statement_line.number = '0001'
        statement_line.description = 'description'
        statement_line.date = today
        statement_line.amount = Decimal('180')
        statement_line.party = customer
        assertEqual(statement_line.account, receivable)
        statement_line.related_to = customer_invoice1
        self.assertEqual(statement_line.amount, Decimal('100.00'))
        statement_line = statement.lines[-1]
        self.assertEqual(statement_line.number, '0001')
        self.assertEqual(statement_line.description, 'description')
        assertEqual(statement_line.party, customer)
        assertEqual(statement_line.account, receivable)
        statement_line.description = 'other description'
        statement_line.related_to = customer_invoice2

        # Paid 50 to customer
        statement_line = StatementLine()
        statement.lines.append(statement_line)
        statement_line.number = '0002'
        statement_line.description = 'description'
        statement_line.date = today
        statement_line.amount = Decimal('-50')
        statement_line.party = customer
        statement_line.account = receivable
        statement_line.related_to = customer_credit_note

        # Paid 50 to supplier
        statement_line = StatementLine()
        statement.lines.append(statement_line)
        statement_line.date = today
        statement_line.amount = Decimal('-60')
        statement_line.party = supplier
        assertEqual(statement_line.account, payable)
        statement_line.related_to = supplier_invoice
        self.assertEqual(statement_line.amount, Decimal('-50.00'))

        # Validate statement
        with self.assertRaises(StatementValidateError):
            statement.click('validate_statement')

        statement_line = StatementLine()
        statement.lines.append(statement_line)
        statement_line.description = 'description2'
        statement_line.date = today
        statement_line.amount = Decimal('30')
        statement_line.party = customer

        # Cancel statement
        statement.click('cancel')
        self.assertEqual(statement.state, 'cancelled')
        self.assertEqual([l.move for l in statement.lines if l.move], [])

        # Reset to draft, validate and post statement
        statement.click('draft')
        self.assertEqual(statement.state, 'draft')
        statement.click('validate_statement')
        self.assertEqual(statement.state, 'validated')
        statement.click('post')
        self.assertEqual(statement.state, 'posted')

        # Test posted moves
        statement_line = statement.lines[0]
        move = statement_line.move
        self.assertEqual(sorted((l.description_used or '' for l in move.lines)),
            ['', 'other description'])
        statement_line = statement.lines[2]
        move = statement_line.move
        self.assertEqual(sorted((l.description_used or '' for l in move.lines)),
            ['', ''])

        # Test invoice state
        customer_invoice1.reload()
        self.assertEqual(customer_invoice1.state, 'posted')
        self.assertEqual(customer_invoice1.amount_to_pay, Decimal('100.00'))
        customer_invoice2.reload()
        self.assertEqual(customer_invoice2.state, 'paid')
        self.assertEqual(customer_invoice2.amount_to_pay, Decimal('0.00'))
        customer_credit_note.reload()
        self.assertEqual(customer_credit_note.state, 'paid')
        supplier_invoice.reload()
        self.assertEqual(supplier_invoice.state, 'paid')
