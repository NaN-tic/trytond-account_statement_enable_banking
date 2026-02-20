import unittest
from decimal import Decimal
from datetime import datetime

from proteus import Model
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.modules.account.tests.tools import create_chart, create_fiscalyear, get_accounts
from trytond.modules.account_invoice.tests.tools import set_fiscalyear_invoice_sequences


class TestEnableBankingScenario(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        config = activate_modules(['account_statement_enable_banking', 'sale'])

        _ = create_company()
        company = get_company()

        User = Model.get('res.user')
        config._context = User.get_preferences(True, config.context)

        fiscalyear = set_fiscalyear_invoice_sequences(
            create_fiscalyear(company))
        fiscalyear.click('create_period')

        _ = create_chart(company)
        accounts = get_accounts(company)
        # add cash account by name (default account_code_digits 8; code 1.1.1000)
        Account = Model.get('account.account')
        account_cash, = Account.find([
            ('name', '=', 'Cash and Cash Equivalents'),
            ], limit=1)
        accounts['cash'] = account_cash

        SequenceType = Model.get('ir.sequence.type')
        seq_type, = SequenceType.find([
                ('name', '=', 'Account Statement Origin'),
                ])

        Sequence = Model.get('ir.sequence')
        sequence = Sequence()
        sequence.name = 'Statement Origin'
        sequence.sequence_type = seq_type
        sequence.company = company
        sequence.save()

        AccountJournal = Model.get('account.journal')
        account_journal = AccountJournal()
        account_journal.name = 'Statement Journal'
        account_journal.type = 'statement'
        account_journal.save()

        Party = Model.get('party.party')
        customer = Party()
        customer.name = 'Customer A'
        customer.save()

        ProductUom = Model.get('product.uom')
        uom, = ProductUom.find([('name', '=', 'Unit')])

        ProductTemplate = Model.get('product.template')
        template = ProductTemplate()
        template.name = 'Product A'
        template.type = 'goods'
        template.default_uom = uom
        template.salable = True
        template.save()
        product, = template.products

        Sale = Model.get('sale.sale')
        sale = Sale()
        sale.company = company
        sale.party = customer
        line = sale.lines.new()
        line.product = product
        line.quantity = 1
        line.unit_price = Decimal('100.00')
        sale.save()
        sale.click('quote')

        Invoice = Model.get('account.invoice')
        invoice = Invoice()
        invoice.company = company
        invoice.party = customer
        line = invoice.lines.new()
        line.account = accounts['revenue']
        line.quantity = 1
        line.unit_price = Decimal('200.00')
        invoice.save()
        invoice.click('post')

        Move = Model.get('account.move')
        move = Move()
        move.company = company
        move.journal = account_journal
        move.date = datetime.today().date()
        move.description = 'Cash'
        line = move.lines.new()
        line.account = accounts['revenue']
        line.debit = Decimal('300.00')
        line = move.lines.new()
        line.account = accounts['receivable']
        line.party = customer
        line.credit = Decimal('300.00')
        move.save()

        StatementJournal = Model.get('account.statement.journal')
        journal = StatementJournal()
        journal.name = 'Bank Statement Journal'
        journal.company = company
        journal.journal = account_journal
        journal.currency = company.currency
        journal.validation = 'balance'
        journal.account = accounts['cash']
        journal.account_statement_origin_sequence = sequence
        journal.save()

        Statement = Model.get('account.statement')
        statement = Statement()
        statement.name = 'Enable Banking Statement'
        statement.journal = journal
        statement.company = company
        statement.date = datetime.today().date()
        statement.start_balance = Decimal('0.00')
        statement.end_balance = Decimal('0.00')
        statement.save()

        Origin = Model.get('account.statement.origin')
        # Hack to make information field writable in test
        Origin._fields['information']['readonly'] = False

        origin1 = Origin()
        origin1.statement = statement
        origin1.date = statement.date
        origin1.amount = Decimal('100.00')
        origin1.description = 'Origin 1'
        origin1.information = {
            'debtor_name': 'Customer A',
            'remittance_information': 'Payment for sale 1',
            }
        self.assertNotEqual(origin1.information, None)
        origin1.save()
        self.assertNotEqual(origin1.information, None)

        origin2 = Origin()
        origin2.statement = statement
        origin2.date = statement.date
        origin2.amount = Decimal('200.00')
        origin2.description = 'Origin 2'
        origin2.information = {
            'debtor_name': 'Customer A',
            'remittance_information': 'Payment for invoice 1',
            }
        origin2.save()

        origin3 = Origin()
        origin3.statement = statement
        origin3.date = statement.date
        origin3.amount = Decimal('300.00')
        origin3.description = 'Origin 3'
        origin3.information = {
            'debtor_name': 'Customer A',
            'remittance_information': 'Unknown payment',
            }

        self.assertEqual(len(statement.origins), 2)

        statement.click('register')

        origin1.click('search_suggestions')
        suggested_line = origin1.suggested_lines[0]
        self.assertEqual(suggested_line.type, 'sale')
        self.assertEqual(len(origin1.lines), 1)
        line, = origin1.lines
        self.assertEqual(line.related_to, sale)
        self.assertEqual(line.amount, Decimal('100.00'))

        origin2.click('search_suggestions')
        suggested_line = origin2.suggested_lines[0]
        self.assertEqual(suggested_line.type, 'balance-invoice')
        self.assertEqual(len(origin2.lines), 1)
        line, = origin2.lines
        self.assertEqual(line.related_to, invoice)
        self.assertEqual(line.amount, Decimal('200.00'))

        origin3.click('search_suggestions')
