# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import requests
from datetime import datetime, UTC, timedelta
from decimal import Decimal
from secrets import token_hex
from itertools import groupby, chain
from sql.functions import Function
from trytond.model import Workflow, ModelView, ModelSQL, fields, tree
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval, Bool, If, PYSON, PYSONEncoder
from trytond.rpc import RPC
from trytond.wizard import (
    Button, StateAction, StateTransition, StateView, Wizard)
from trytond.transaction import Transaction
from trytond.config import config
from .common import get_base_header
from trytond.i18n import gettext
from trytond.model.exceptions import AccessError
from trytond.modules.account_statement.exceptions import (
    StatementValidateError, StatementValidateWarning)
from trytond.modules.currency.fields import Monetary
from trytond.modules.account_statement.statement import Unequal
from trytond import backend


_ZERO = Decimal(0)


class Similarity(Function):
    __slots__ = ()
    _function = 'SIMILARITY'


class JsonbExtractPathText(Function):
    __slots__ = ()
    _function = 'JSONB_EXTRACT_PATH_TEXT'


class Statement(metaclass=PoolMeta):
    __name__ = 'account.statement'

    start_date = fields.DateTime("Start Date", readonly=True)
    end_date = fields.DateTime("End Date", readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()

        if cls.date.states.get('invisible', None):
            cls.date.states['invisible'] |= (Bool(Eval('start_date')))
        else:
            cls.date.states['invisible'] = Bool(Eval('start_date'))
        # Add new state to the statement, to avoid some checks qhen the
        # statement came from the Bank lines.
        cls.state.selection.append(('registered', "Registered"))
        cls._transitions |= set((
                ('draft', 'registered'),
                ('registered', 'draft'),
                ('registered', 'validated'),
                ('validated', 'registered'),
                ))
        cls._buttons.update({
                'register': {
                    'invisible': ~Eval('state').in_(['draft', 'validated']),
                    'depends': ['state'],
                    },
                })
        cls._buttons['draft']['invisible'] = ~Eval('state').in_(
            ['cancelled', 'registered'])
        cls._buttons['validate_statement']['invisible'] = ~Eval('state').in_(
            ['draft', 'registered'])

    def _group_key(self, line):
        pool = Pool()
        StatementOrigin = pool.get('account.statement.origin')
        StatementLine = pool.get('account.statement.line')

        one_move = (line.statement.journal.one_move_per_origin
            if line.statement else None)
        if one_move and isinstance(line, (StatementLine, StatementOrigin)):
            if isinstance(line, StatementLine):
                key = (
                    ('number', line.origin and (
                            line.origin.number or line.origin.description)
                        or Unequal()),
                    ('date', line.origin.date),
                    ('origin', line.origin),
                    )
            elif isinstance(line, StatementOrigin):
                key = (
                    ('number', (line.number or line.description) or Unequal()),
                    ('date', line.date),
                    ('origin', line),
                    )
        else:
            key = super()._group_key(line)
        return key

    @classmethod
    def cancel(cls, statements):
        pool = Pool()
        Origin = pool.get('account.statement.origin')

        origins = [o for s in statements for o in s.origins]
        if origins:
            Origin.cancel(origins)

        super().cancel(statements)

    @classmethod
    @ModelView.button
    @Workflow.transition('registered')
    def register(cls, statements):
        pass


_states = {
    'readonly': ~Eval('statement_state', '').in_(['draft', 'registered'])
    }


class Line(metaclass=PoolMeta):
    __name__ = 'account.statement.line'

    maturity_date = fields.Date("Maturity Date",
        states={
            'invisible': Bool(Eval('related_to')),
            'readonly': Eval('origin_state') != 'registered',
            },
        depends=['related_to'],
        help="Set a date to make the line payable or receivable.")
    suggested_line = fields.Many2One('account.statement.origin.suggested.line',
        'Suggested Lines',
        states={
            'readonly': Eval('origin_state') != 'registered',
            })
    origin_state = fields.Function(
        fields.Selection('get_origin_states', "Origin State"),
        'on_change_with_origin_state')
    show_paid_invoices = fields.Boolean('Show Paid Invoices',
        states={
            'readonly': Eval('origin_state') != 'registered',
            })

    @classmethod
    def __setup__(cls):
        super().__setup__()

        new_domain = []
        for domain in cls.related_to.domain['account.invoice']:
            if isinstance(domain, PYSON):
                values = [x for x in domain.pyson().values()
                    if isinstance(x, tuple)]
                if ('state', '=', 'posted') in values:
                    new_domain.append(
                        If(Bool(Eval('show_paid_invoices')),
                            ('state', '=', 'paid'),
                            If(Eval('statement_state').in_(
                                    ['draft', 'registered']),
                                ('state', '=', 'posted'),
                                ('state', '!=', ''))))
                    continue
            new_domain.append(domain)
        cls.related_to.domain['account.invoice'] = new_domain
        cls.related_to.domain['account.move.line'] = [
            ('company', '=', Eval('company', -1)),
            If(Eval('second_currency'),
                ('second_currency', '=', Eval('second_currency', -1)),
                ('currency', '=', Eval('currency', -1)),
               ),
            If(Bool(Eval('party')),
                ('party', '=', Eval('party')),
                ()),
            If(Bool(Eval('account')),
                ('account', '=', Eval('account')),
                ()),
            ('move_state', '=', 'posted'),
            ('account.reconcile', '=', True),
            ('state', '=', 'valid'),
            ('reconciliation', '=', None),
            ('invoice_payment', '=', None),
            ]
        cls.statement.states['readonly'] = _states['readonly']
        cls.number.states['readonly'] = _states['readonly']
        cls.date.states['readonly'] = (
            _states['readonly'] | Bool(Eval('origin', 0))
            )
        cls.amount.states['readonly'] = _states['readonly']
        cls.amount_second_currency.states['readonly'] = _states['readonly']
        cls.second_currency.states['readonly'] = _states['readonly']
        cls.party.states['readonly'] = _states['readonly']
        cls.party.states['required'] = (Eval('party_required', False)
            & (Eval('statement_state').in_(['draft', 'registered']))
            )
        cls.account.states['readonly'] = _states['readonly']
        cls.description.states['readonly'] = _states['readonly']
        cls.related_to.states['readonly'] = _states['readonly']

    @classmethod
    def _get_relations(cls):
        return super()._get_relations() + ['account.move.line']

    @classmethod
    def get_origin_states(cls):
        pool = Pool()
        Origin = pool.get('account.statement.origin')
        return Origin.fields_get(['state'])['state']['selection']

    @fields.depends('origin', '_parent_origin.state')
    def on_change_with_origin_state(self, name=None):
        if self.origin:
            return self.origin.state

    @fields.depends('origin', 'related_to', '_parent_origin.second_currency')
    def on_change_with_second_currency(self, name=None):
        if not self.related_to:
            return None
        if self.origin and self.origin.second_currency:
            return self.origin.second_currency

    @fields.depends('origin', 'related_to',
        '_parent_origin.amount_second_currency')
    def on_change_with_amount_second_currency(self, name=None):
        if not self.related_to:
            return None
        if self.origin and self.origin.amount_second_currency:
            return self.origin.amount_second_currency

    @property
    @fields.depends('related_to')
    def move_line(self):
        pool = Pool()
        MoveLine = pool.get('account.move.line')

        related_to = getattr(self, 'related_to', None)
        if isinstance(related_to, MoveLine) and related_to.id >= 0:
            return related_to

    @move_line.setter
    def move_line(self, value):
        self.related_to = value

    @property
    @fields.depends('related_to')
    def move_line_invoice(self):
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        Invoice = pool.get('account.invoice')

        related_to = getattr(self, 'related_to', None)
        if (isinstance(related_to, MoveLine) and related_to.id >= 0
                and related_to.move and related_to.move.origin
                and isinstance(related_to.move.origin, Invoice)):
            return related_to.move.origin

    @property
    @fields.depends('company', '_parent_company.currency',
        'show_paid_invoices',
        methods=['invoice', 'move_line_invoice'])
    def invoice_amount_to_pay(self):
        amount_to_pay = None
        # control the possibilty to use the move from invoice
        invoice = self.invoice or self.move_line_invoice or None
        if invoice:
            sign = -1 if invoice.type == 'in' else 1
            if invoice.currency == self.company.currency:
                # If we are in the case that need control a refund invoice,
                # need to get the total amount of the invoice.
                amount_to_pay = sign * (invoice.total_amount
                    if self.show_paid_invoices and invoice.state == 'paid'
                    else invoice.amount_to_pay)
            else:
                amount = _ZERO
                if invoice.state == 'posted':
                    for line in (invoice.lines_to_pay
                            + invoice.payment_lines):
                        if line.reconciliation:
                            continue
                        amount += line.debit - line.credit
                else:
                    # If we are in the case that need control a refund invoice,
                    # need to get the total amount of the invoice.
                    amount = (sign * invoice.total_amount
                        if self.show_paid_invoices and invoice.state == 'paid'
                        else _ZERO)
                amount_to_pay = amount
        if self.show_paid_invoices and amount_to_pay:
            amount_to_pay = -1 * amount_to_pay
        return amount_to_pay

    @fields.depends('show_paid_invoices')
    def on_change_party(self):
        if not self.show_paid_invoices:
            super().on_change_party()

    @fields.depends('amount', 'account', methods=['invoice', 'move_line',
        'invoice_amount_to_pay'])
    def on_change_amount(self):
        if self.invoice:
            if self.invoice.account != self.account:
                self.account = self.invoice.account
            if (self.amount is not None
                    and self.invoice_amount_to_pay is not None
                    and ((self.amount >= 0) != (
                            self.invoice_amount_to_pay >= 0)
                        or (self.amount >= 0
                            and self.amount > self.invoice_amount_to_pay)
                        or (self.amount < 0
                            and self.amount < self.invoice_amount_to_pay))):
                self.amount = self.invoice_amount_to_pay
        elif self.move_line:
            if self.move_line.account != self.account:
                self.account = self.move_line.account
            if (self.amount is not None and self.move_line.amount is not None
                    and ((self.amount >= 0) != (
                            self.move_line.amount >= 0)
                        or (self.amount >= 0
                            and self.amount > self.move_line.amount)
                        or (self.amount < 0
                            and self.amount < self.move_line.amount))):
                self.amount = self.move_line.amount
        else:
            super().on_change_amount()

    @fields.depends('account', methods=['move_line'])
    def on_change_account(self):
        super().on_change_account()
        if self.move_line:
            if self.account:
                if self.move_line.account != self.account:
                    self.move_line = None
            else:
                self.move_line = None

    @fields.depends('related_to', 'party', 'description', 'show_paid_invoices',
        'origin', '_parent_origin.information', 'company',
        '_parent_origin.remittance_information', '_parent_company.currency',
        methods=['move_line', 'payment', 'invoice_amount_to_pay'])
    def on_change_related_to(self):
        pool = Pool()
        Invoice = pool.get('account.invoice')

        super().on_change_related_to()
        if self.move_line:
            if not self.party:
                self.party = self.move_line.party
            if not self.description:
                self.description = (self.move_line.description
                    or self.move_line.move_description_used)
            self.account = self.move_line.account
        related_to = getattr(self, 'related_to', None)
        if self.show_paid_invoices and not isinstance(related_to, Invoice):
            self.show_paid_invoices = False

        if not self.description and self.origin and self.origin.information:
            self.description = self.origin.remittance_information

        # TODO: Control when the currency is different
        payments = set()
        move_lines = set()
        invoice_id2amount_to_pay = {}
        if self.invoice and self.invoice.id not in invoice_id2amount_to_pay:
            invoice_id2amount_to_pay[self.invoice.id] = (
                self.invoice_amount_to_pay)
        if self.payment and self.payment.currency == self.company.currency:
            payments.add(self.payment)
        if self.move_line and self.move_line.currency == self.company.currency:
            move_lines.add(self.move_line)

        payment_id2amount = (dict((x.id, x.amount) for x in payments)
            if payments else {})

        move_line_id2amount = (dict((x.id, x.amount) for x in move_lines)
            if move_lines else {})

        # As a 'core' difference, the value of the line amount must be the
        # amount of the movement, invoice or payment. Not the line amount
        # pending. It could induce an incorrect concept nad misunderstunding.
        amount = None
        if self.invoice and self.invoice.id in invoice_id2amount_to_pay:
            amount = invoice_id2amount_to_pay.get(
                self.invoice.id, _ZERO)
        if self.payment and self.payment.id in payment_id2amount:
            amount = payment_id2amount[self.payment.id]
        if self.move_line and self.move_line.id in move_line_id2amount:
            amount = move_line_id2amount[self.move_line.id]
        if amount is None and self.invoice:
            self.invoice = None
        if amount is None and self.payment:
            self.payment = None
        if amount is None and self.move_line:
            self.move_line = None
        self.amount = amount

    @classmethod
    def cancel_move(cls, moves):
        pool = Pool()
        Move = pool.get('account.move')
        MoveLine = pool.get('account.move.line')
        Reconciliation = pool.get('account.move.reconciliation')
        Invoice = pool.get('account.invoice')

        for move in moves:
            to_unreconcile = [x.reconciliation for x in move.lines
                if x.reconciliation]
            if to_unreconcile:
                to_unreconcile = Reconciliation.browse([
                        x.id for x in to_unreconcile])
                Reconciliation.delete(to_unreconcile)

            # On possible related invoices, need to unlink the payment
            # lines
            to_unpay = [x for x in move.lines if x.invoice_payment]
            if to_unpay:
                Invoice.remove_payment_lines(to_unpay)

            cancel_move = move.cancel(reversal=True)
            cancel_move.origin = move.origin
            Move.post([cancel_move])
            mlines = [l for m in [move, cancel_move]
                for l in m.lines if l.account.reconcile]
            mlines.sort(key=lambda x: (x.party, x.account))
            mlines = [list(l) for _, l in groupby(mlines,
                    key=lambda x: (x.party, x.account))]
            if mlines:
                MoveLine.reconcile(*mlines)

    @classmethod
    def cancel_lines(cls, lines, origin=None):
        '''As is needed save an history fo all movements, do not remove the
        possible move related. Create the cancelation move and leave they
        related to the statement and the origin, to have an hstory.
        '''
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        SuggestedLine = pool.get('account.statement.origin.suggested.line')
        Warning = pool.get('res.user.warning')

        moves = set()
        mlines = []
        for line in lines:
            if line.move:
                warning_key = Warning.format(
                    'origin_line_with_move', [line.move.id])
                if Warning.check(warning_key):
                    raise StatementValidateWarning(warning_key,
                        gettext('account_statement_enable_banking.'
                            'msg_origin_line_with_move',
                            move=line.move.rec_name))
                for mline in line.move.lines:
                    if mline.origin == line:
                        mlines.extend(([mline], {'origin': line.origin}))
                moves.add(line.move)
        if mlines:
            with Transaction().set_context(from_account_statement_origin=True):
                MoveLine.write(*mlines)
        cls.cancel_move(list(moves))

        suggested_lines = [x.suggested_line for x in lines
            if x.suggested_line]
        suggested_lines.extend(list(set([x.parent
                        for x in suggested_lines if x.parent])))
        cls.write(lines, {'suggested_line': None})
        if suggested_lines and (origin is None or origin == 'delete_move'):
            SuggestedLine.propose(suggested_lines)
        elif suggested_lines:
            SuggestedLine.delete(suggested_lines)

    @classmethod
    def reconcile(cls, move_lines):
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        Reconcile = pool.get('account.move.reconciliation')
        Invoice = pool.get('account.invoice')
        Payment = pool.get('account.payment')

        to_reconcile = {}
        invoice_to_save = []
        move_to_reconcile = {}
        statement_lines = []
        for move_line, statement_line in move_lines:
            if not statement_line:
                continue
            if (statement_line.invoice and statement_line.show_paid_invoices
                    and move_line.account == statement_line.invoice.account):
                additional_moves = [move_line.move]
                invoice = statement_line.invoice
                reconcile = [move_line]
                payment_lines = list(set(chain(
                            [x for x in invoice.payment_lines],
                            invoice.reconciliation_lines)))
                payments = []
                for line in payment_lines:
                    payments.extend([p for l in line.reconciliation.lines
                            for p in l.payments if l.id != line.id])
                    # Temporally, need to allow
                    # from_account_bank_statement_line, until all is move
                    # from the old bank_statement to the new statement.
                    with Transaction().set_context(_skip_warnings=True,
                            from_account_bank_statement_line=True):
                        Reconcile.delete([line.reconciliation])
                    if line.move not in invoice.additional_moves:
                        additional_moves.append(line.move)
                    reconcile.append(line)
                if payments:
                    Payment.fail(payments)
                if reconcile:
                    MoveLine.reconcile(reconcile)
                if invoice.payment_lines:
                    invoice.payment_lines = None
                    invoice_to_save.append(invoice)
                if additional_moves:
                    invoice.additional_moves += tuple(additional_moves)
                    invoice_to_save.append(invoice)
            elif statement_line.invoice:
                key = (statement_line.party, statement_line.invoice)
                if key in to_reconcile:
                    to_reconcile[key].append((move_line, statement_line))
                else:
                    to_reconcile[key] = [(move_line, statement_line)]
            elif statement_line.move_line:
                assert move_line.account == statement_line.move_line.account
                key = statement_line.party
                if key in move_to_reconcile:
                    move_to_reconcile[key].append(
                        (move_line, statement_line.move_line))
                else:
                    move_to_reconcile[key] = [
                        (move_line, statement_line.move_line)]
            statement_lines.append(statement_line.id)
        if invoice_to_save:
            Invoice.save(list(set(invoice_to_save)))
        if to_reconcile:
            for _, value in to_reconcile.items():
                super().reconcile(value)
        if move_to_reconcile:
            with Transaction().set_context(
                    account_statement_lines=statement_lines):
                for _, value in move_to_reconcile.items():
                    MoveLine.reconcile(*value)

    @classmethod
    def delete(cls, lines):
        cls.cancel_lines(lines, origin='delete')
        for line in lines:
            if line.statement_state not in {
                    'cancelled', 'registered', 'draft'}:
                raise AccessError(
                    gettext(
                        'account_statement.'
                        'msg_statement_line_delete_cancel_draft',
                        line=line.rec_name,
                        sale=line.statement.rec_name))
        # Use __func__ to directly access ModelSQL's delete method and
        # pass it the right class
        ModelSQL.delete.__func__(cls, lines)

    @classmethod
    def delete_move(cls, lines):
        cls.cancel_lines(lines, origin='delete_move')
        super().delete_move(lines)

    def get_move_line(self):
        line = super().get_move_line()
        if self.maturity_date:
            line.maturity_date = self.maturity_date
        return line

    @classmethod
    def copy(cls, lines, default=None):
        default = default.copy() if default is not None else {}
        default.setdefault('maturity_date', None)
        default.setdefault('suggested_line', None)
        default.setdefault('show_paid_invoices', None)
        return super().copy(lines, default=default)


class Origin(Workflow, metaclass=PoolMeta):
    __name__ = 'account.statement.origin'

    entry_reference = fields.Char("Entry Reference", readonly=True)
    suggested_lines = fields.One2Many(
        'account.statement.origin.suggested.line', 'origin',
        'Suggested Lines', states=_states)
    suggested_lines_tree = fields.Function(
        fields.Many2Many('account.statement.origin.suggested.line', None, None,
            'Suggested Lines', states=_states), 'get_suggested_lines_tree')
    balance = Monetary("Balance", currency='currency', digits='currency',
        readonly=True)
    state = fields.Selection([
            ('registered', "Registered"),
            ('cancelled', "Cancelled"),
            ('posted', "Posted"),
            ], "State", readonly=True, required=True, sort=False)
    remittance_information = fields.Function(
        fields.Char('Remittance Information'), 'get_remittance_information',
        searcher='search_remittance_information')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.number.search_unaccented = False
        cls._order.insert(0, ('date', 'ASC'))
        cls._order.insert(1, ('number', 'ASC'))
        cls.statement.states['readonly'] = _states['readonly']
        cls.number.states['readonly'] = _states['readonly']
        cls.date.states['readonly'] = _states['readonly']
        cls.amount.states['readonly'] = _states['readonly']
        cls.amount_second_currency.states['readonly'] = _states['readonly']
        cls.second_currency.states['readonly'] = _states['readonly']
        cls.party.states['readonly'] = _states['readonly']
        cls.account.states['readonly'] = _states['readonly']
        cls.description.states['readonly'] = _states['readonly']
        cls.lines.states['readonly'] = (
            (Eval('statement_id', -1) < 0)
            | (~Eval('statement_state').in_(
                    ['draft', 'registered', 'validated']))
            )
        cls._transitions |= set((
                ('registered', 'posted'),
                ('registered', 'cancelled'),
                ('cancelled', 'registered'),
                ('posted', 'cancelled'),
                ))
        cls._buttons.update({
                'multiple_invoices': {
                    'invisible': Eval('state') != 'registered',
                    'depends': ['state'],
                    },
                'multiple_move_lines': {
                    'invisible': Eval('state') != 'registered',
                    'depends': ['state'],
                    },
                'register': {
                    'invisible': Eval('state') != 'cancelled',
                    'depends': ['state'],
                    },
                'post': {
                    'invisible': Eval('state') != 'registered',
                    'depends': ['state'],
                    },
                'cancel': {
                    'invisible': Eval('state') == 'cancelled',
                    'depends': ['state'],
                    },
                'search_suggestions': {
                    'invisible': Eval('state') != 'registered',
                    'depends': ['state'],
                    },
                })
        cls.__rpc__.update({
                'post': RPC(
                    readonly=False, instantiate=0, fresh_session=True),
                })

    @staticmethod
    def default_state():
        return 'registered'

    @fields.depends('state')
    def on_change_with_statement_state(self, name=None):
        try:
            state = super().on_change_with_statement_state()
        except AttributeError:
            state = None
        return self.state or state

    @property
    @fields.depends('statement', '_parent_statement.journal')
    def similarity_threshold(self):
        return (self.statement.journal.similarity_threshold
            if self.statement and self.statement.journal else None)

    @property
    @fields.depends('statement', '_parent_statement.journal')
    def acceptable_similarity(self):
        return (self.statement.journal.acceptable_similarity
            if self.statement and self.statement.journal else None)

    def get_suggested_lines_tree(self, name):
        # return only parent lines in origin suggested lines
        # Not children.
        suggested_lines = []

        def _get_children(line):
            if line.parent is None:
                suggested_lines.append(line)

        for line in self.suggested_lines:
            _get_children(line)

        return [x.id for x in suggested_lines if x.state == 'proposed']

    def get_remittance_information(self, name):
        return (self.information.get('remittance_information', '')
            if self.information else '')

    @classmethod
    def search_remittance_information(cls, name, clause):
        pool = Pool()
        StatementOrigin = pool.get('account.statement.origin')

        origin_table = StatementOrigin.__table__()
        cursor = Transaction().connection.cursor()
        _, operator, value = clause
        operator = 'in' if value else 'not in'

        if backend.name == 'postgresql':
            remittance_information_column = JsonbExtractPathText(
                    origin_table.information, 'remittance_information')
        else:
            remittance_information_column = origin_table.information
        query = origin_table.select(origin_table.id,
            where=(remittance_information_column.ilike(value)))
        cursor.execute(*query)
        return [('id', operator, [x[0] for x in cursor.fetchall()])]

    def validate_amount(self):
        pool = Pool()
        Lang = pool.get('ir.lang')

        amount = sum(x.amount for x in self.lines)
        if amount != self.amount:
            lang = Lang.get()
            total_amount = lang.currency(
                self.amount, self.statement.journal.currency)
            amount = lang.currency(amount, self.statement.journal.currency)
            raise StatementValidateError(
                gettext('account_statement_enable_banking.'
                    'msg_origin_pending_amount',
                    origin_amount=total_amount,
                    line_amount=amount))

    @classmethod
    def validate_origin(cls, origins):
        '''Basically is a piece of copy & paste from account_statement
        validate_statement(), but adapted to work at origin level
        '''
        pool = Pool()
        StatementLine = pool.get('account.statement.line')
        Invoice = pool.get('account.invoice')
        Warning = pool.get('res.user.warning')

        paid_cancelled_invoice_lines = []
        for origin in origins:
            origin.validate_amount()

            for line in origin.lines:
                if line.related_to:
                    repeated = StatementLine.search([
                            ('related_to', '=', line.related_to),
                            ('id', '!=', line.id),
                            ('origin.state', '=', 'posted'),
                            ('show_paid_invoices', '=', False),
                            ])
                    if not repeated and line.invoice:
                        repeated = StatementLine.search([
                                ('related_to', 'in',
                                    line.invoice.lines_to_pay),
                                ('origin.state', '=', 'posted'),
                                ])
                    if (not repeated and line.move_line
                            and line.move_line.move_origin
                            and isinstance(
                                line.move_line.move_origin, Invoice)):
                        repeated = StatementLine.search([
                                ('related_to', '=',
                                    line.move_line.move_origin),
                                ('origin.state', '=', 'posted'),
                                ])
                    if repeated:
                        invoice_amount_to_pay = line.invoice_amount_to_pay
                        if line.invoice and line.show_paid_invoices:
                            # returned recipt
                            # Unlink the account move from the account statement line
                            # to allow correctly attach to another statement line.
                            StatementLine.write(repeated, {
                                    'related_to': None,
                                    })
                            continue
                        elif invoice_amount_to_pay is not None:
                            # partial payment
                            line_sign = 1 if line.amount >= 0 else 0
                            invoice_sign = (1
                                if invoice_amount_to_pay >= 0 else 0)
                            if (line_sign == invoice_sign
                                    and abs(line.amount) <= abs(
                                        invoice_amount_to_pay)):
                                continue
                        raise AccessError(
                            gettext('account_statement_enable_banking.'
                                'msg_repeated_related_to_used',
                                related_to=str(line.related_to),
                                origin=(repeated[0].origin.rec_name
                                    if repeated[0].origin else '')))
            paid_cancelled_invoice_lines.extend(x for x in origin.lines
                if x.invoice and (x.invoice.state == 'cancelled'
                    or (x.invoice.state == 'paid'
                        and not x.show_paid_invoices)))

        if paid_cancelled_invoice_lines:
            warning_key = Warning.format(
                'statement_paid_cancelled_invoice_lines',
                paid_cancelled_invoice_lines)
            if Warning.check(warning_key):
                raise StatementValidateWarning(warning_key,
                    gettext('account_statement'
                        '.msg_statement_invoice_paid_cancelled'))
            StatementLine.write(paid_cancelled_invoice_lines, {
                    'related_to': None,
                    })

    @classmethod
    def create_moves(cls, origins):
        '''Basically is a copy & paste from account_statement create_move(),
        but adapted to work at origin level
        '''
        pool = Pool()
        StatementLine = pool.get('account.statement.line')
        StatementSuggest = pool.get('account.statement.origin.suggested.line')
        Move = pool.get('account.move')
        MoveLine = pool.get('account.move.line')

        moves = []
        lines_to_check = []
        for origin in origins:
            for key, lines in groupby(
                    origin.lines, key=origin.statement._group_key):
                lines = list(lines)
                lines_to_check.extend(lines)
                key = dict(key)
                move = origin.statement._get_move(key)
                move.origin = origin
                moves.append((move, lines))

        Move.save([m for m, _ in moves])

        to_write = []
        for move, lines in moves:
            to_write.extend((lines, {'move': move.id}))
        if to_write:
            StatementLine.write(*to_write)

        move_lines = []
        for move, lines in moves:
            amount = _ZERO
            amount_second_currency = _ZERO
            statement = lines[0].statement if lines else None
            for line in lines:
                move_line = line.get_move_line()
                if not move_line:
                    continue
                move_line.move = move
                amount += move_line.debit - move_line.credit
                if move_line.amount_second_currency:
                    amount_second_currency += move_line.amount_second_currency
                move_lines.append((move_line, line))

            if statement:
                move_line = statement._get_move_line(
                    amount, amount_second_currency, lines)
                move_line.move = move
                move_lines.append((move_line, None))

        if move_lines:
            MoveLine.save([x for x, _ in move_lines])

        # Ensure that any related_to posted lines are not in another registered
        # origin or suggested. Except for the paid invoice process or the
        #  partial payment invoices.
        if lines_to_check:
            related_tos = []
            line_ids = []
            suggested_ids = []
            for line in lines_to_check:
                # returned recipt
                if line.show_paid_invoices:
                    continue
                # partial payment. In this point the invoice is payed or
                # partial payed, so the invoice.amount_to_pay >= 0.
                if line.invoice and line.invoice.amount_to_pay >= 0:
                    continue
                line_ids.append(line.id)
                if line.related_to:
                    related_tos.append(line.related_to)
                if line.suggested_line:
                    suggested_ids.append(line.suggested_line.id)
            lines = StatementLine.search([
                    ('related_to', 'in', related_tos),
                    ('id', 'not in', line_ids),
                    ('show_paid_invoices', '=', False),
                    ('origin', '!=', None),
                    ])
            lines_not_allowed = [l for l in lines if l.origin.state == 'posted']
            lines_to_remove = [l for l in lines if l.origin.state != 'posted']
            if lines_not_allowed:
                raise AccessError(
                    gettext('account_statement_enable_banking.'
                        'msg_repeated_related_to_used',
                        realted_to=lines_not_allowed[0].related_to,
                        origin=(lines_not_allowed[0].origin.rec_name
                            if lines_not_allowed[0].origin else '')))
            if lines_to_remove:
                StatementLine.delete(lines_to_remove)

            suggest_to_remove = StatementSuggest.search([
                    ('related_to', 'in', related_tos),
                    ('id', 'not in', suggested_ids),
                    ])
            if suggest_to_remove:
                StatementSuggest.delete(suggest_to_remove)
        # Reconcile at the end to avoid problems with the related_to lines
        if move_lines:
            StatementLine.reconcile(move_lines)
        return moves

    def similarity_parties(self, compare, similarity_threshold=0.13):
        """
        This function return a dictionary with the possible parties ID on
        'key' and the similairty on 'value'.
        It compare the 'compare' value with the parties name, based on the
        similarities journal deffined values.
        Set the similarity threshold to 0.13 as is the minimum value detecte
        that return a correct match wiht multiples words in compare field.
        """
        pool = Pool()
        Party = pool.get('party.party')
        party_table = Party.__table__()
        cursor = Transaction().connection.cursor()

        if not compare:
            return

        similarity_parties = {}
        similarity_party = Similarity(party_table.name, compare)
        if hasattr(Party, 'trade_name'):
            similarity_party_trade = Similarity(party_table.trade_name,
                compare)
            where = ((similarity_party >= similarity_threshold) | (
                    (party_table.trade_name != None)
                    & (similarity_party_trade >= similarity_threshold)))
        else:
            where = (similarity_party >= similarity_threshold)
        query = party_table.select(party_table.id, similarity_party,
            where=where)
        cursor.execute(*query)
        for similarity in cursor.fetchall():
            similarity_parties[similarity[0]] = round(similarity[1] * 10)
        if not similarity_parties:
            compare_split = compare.split()
            if len(compare_split) > 1:
                for compare in compare_split:
                    self.similarity_parties(compare, 0.3)
        return similarity_parties

    def increase_similarity_by_interval_date(self, date, interval_date=None,
            similarity=0):
        """
        This funtion increase the similarity if the dates are equal or in the
        interval.
        """
        if date:
            control_dates = [self.date]
            if self.information and self.information.get('value_date'):
                control_dates.append(datetime.strptime(
                        self.information['value_date'], '%Y-%m-%d').date())
            if not interval_date:
                interval_date = timedelta(days=3)
            if date in control_dates:
                similarity += 3
            else:
                for control_date in control_dates:
                    start_date = control_date - interval_date
                    end_date = control_date + interval_date
                    if start_date <= date <= end_date:
                        similarity += 2
                        break
        return similarity

    def increase_similarity_by_party(self, party, similarity_parties,
            similarity=0):
        """
        This funtion increase the similarity if the party are similar.
        """
        if party:
            party_id = party.id
            if party_id in similarity_parties:
                if similarity_parties[party_id] >= self.acceptable_similarity:
                    similarity += 3
                else:
                    similarity += 2
        return similarity

    def _get_suggested_values(self, parent, name, line, amount, related_to,
            similarity):
        second_currency = self.second_currency
        amount_second_currency = self.amount_second_currency
        if (hasattr(line, 'payments') and not line.payments
                and line.second_currency != self.currency):
            second_currency = line.second_currency
            amount_second_currency = line.amount_second_currency
        if hasattr(line, 'maturity_date'):
            date = line.maturity_date or line.date
        else:
            date = self.date
        values = {
            'name': '' if parent else name,
            'parent': parent,
            'origin': self,
            'party': line.party,
            'date': date,
            'related_to': related_to,
            'account': line.account,
            'amount': amount,
            'second_currency': second_currency,
            'amount_second_currency': amount_second_currency,
            'similarity': similarity,
            'state': 'proposed',
            }
        return values

    def create_payment_suggested_line(self, move_lines, amount, name,
            payment=False, similarity=0):
        """
        Create one or more suggested registers based on the move_lines.
        If there are more than one move_line, it will be grouped under
        a parent.
        """
        pool = Pool()
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        parent = None
        to_create = []
        if not move_lines:
            return parent, to_create
        elif len(move_lines) > 1:
            parent = SuggestedLine()
            parent.origin = self
            parent.name = name
            parent.amount = amount
            parent.state = 'proposed'
            parent.similarity = similarity
            parent.save()

        for line in move_lines:
            if payment and line.payments:
                if not parent and not name:
                    name = line.payments[0].rec_name
                related_to = line.payments[0]
            else:
                accepted_origins = SuggestedLine.related_to.domain.keys()
                related_to = (line.move_origin if line.move_origin
                    and line.move_origin.__name__ in accepted_origins
                    else line)
                if not parent and not name:
                    name = line.rec_name
            amount = line.debit - line.credit
            values = self._get_suggested_values(parent, name, line, amount,
                related_to, similarity)
            to_create.append(values)
        return parent, to_create

    def create_move_suggested_line(self, move_lines, amount, name,
            similarity=0):
        """
        Create one or more suggested registers based on the move_lines.
        If there are more than one move_line, it will be grouped under
        parent.
        """
        pool = Pool()
        SuggestedLine = pool.get('account.statement.origin.suggested.line')
        Invoice = pool.get('account.invoice')

        parent = None
        to_create = []
        if not move_lines:
            return parent, to_create
        elif len(move_lines) > 1:
            parent = SuggestedLine()
            parent.origin = self
            parent.name = name
            parent.amount = amount
            parent.state = 'proposed'
            parent.similarity = similarity
            parent.save()
        for line in move_lines:
            related_to = (line.move_origin if line.move_origin
                and isinstance(line.move_origin, Invoice)
                and line.move_origin.state != 'paid' else line)
            amount = line.debit - line.credit
            values = self._get_suggested_values(parent, name, line, amount,
                related_to, similarity)
            to_create.append(values)
        return parent, to_create

    def _search_clearing_payment_group_reconciliation_domain(self, amount=None,
            kind=None):
        domain = [
            ('journal.currency', '=', self.currency),
            ('journal.clearing_account', '!=', None),
            ('company', '=', self.company.id),
            ]
        if amount:
            domain.append(('total_amount', '=', amount))
        if kind is not None:
            domain.append(('kind', '=', kind))

        return domain

    def _search_suggested_reconciliation_clearing_payment_group(self, amount,
            acceptable=0):
        pool = Pool()
        Group = pool.get('account.payment.group')

        suggesteds = []
        groups = []

        if not amount:
            return suggesteds, groups

        kind = 'receivable' if amount > _ZERO else 'payable'
        domain = self._search_clearing_payment_group_reconciliation_domain(
            abs(amount), kind)

        for group in Group.search(domain):
            found = True
            for payment in group.payments:
                if (payment.state == 'failed' or (payment.state != 'failed'
                            and payment.line and payment.line.reconciliation)):
                    found = False
                    break
            if found:
                groups.append(group)

        for group in groups:
            similarity = self.increase_similarity_by_interval_date(
                group.planned_date, similarity=acceptable)
            values = {
                'name': group.rec_name,
                'origin': self,
                'date': group.planned_date,
                'related_to': group,
                'amount': amount,
                'account': group.journal.clearing_account,
                'second_currency': self.second_currency,
                'similarity': similarity,
                'state': 'proposed',
                }
            suggesteds.append(values)
        return suggesteds, groups

    def _search_clearing_payment_reconciliation_domain(self, amount=None,
            exclude=None):
        domain = [
            ('currency', '=', self.currency),
            ('company', '=', self.company.id),
            ('state', '!=', 'failed'),
            ('journal.clearing_account', '!=', None),
            ('clearing_move', '!=', None),
            ]
        if amount:
            domain.append(('total_amount', '=', amount))
        if exclude:
            domain.append(('group', 'not in', exclude))
        return domain

    def _search_suggested_reconciliation_clearing_payment(self, amount,
            acceptable=0, parties=None, exclude=None):
        pool = Pool()
        Payment = pool.get('account.payment')

        suggesteds = []
        move_lines = []

        if not amount:
            return suggesteds, move_lines

        domain = self._search_clearing_payment_reconciliation_domain(amount,
            exclude)
        for payment in Payment.search(domain):
            name = (payment.group.rec_name
                if payment.group
                else gettext('account_statement_enable_banking.msg_payments'))
            move_lines.append(payment.line)
            similarity = self.increase_similarity_by_interval_date(
                payment.date, similarity=acceptable)
            party = payment.party
            if party:
                similarity = self.increase_similarity_by_party(
                    party, parties, similarity=similarity)
            parent, to_create = self.create_payment_suggested_line(
                move_lines, amount, name=name, payment=True,
                similarity=similarity)
            suggesteds.extend(to_create)
        return suggesteds, move_lines

    def _search_payment_reconciliation_domain(self, exclude_groups=None,
            exclude_lines=None):
        domain = [
            ('currency', '=', self.currency),
            ('company', '=', self.company.id),
            ('state', '!=', 'failed'),
            ('line', '!=', None),
            ('line.reconciliation', '=', None),
            ('line.account.reconcile', '=', True),
            ]
        if exclude_groups:
            domain.append(('group', 'not in', exclude_groups))
        if exclude_lines:
            domain.append(('line', 'not in', exclude_lines))
        return domain

    def _search_suggested_reconciliation_payment(self, amount, acceptable=0,
            parties=None, exclude_groups=None, exclude_lines=None):
        pool = Pool()
        Payment = pool.get('account.payment')

        suggesteds = []
        move_lines = []

        if not amount:
            return suggesteds, move_lines

        domain = self._search_payment_reconciliation_domain(exclude_groups,
            exclude_lines)

        groups = {
            'amount': _ZERO,
            'groups': {}
            }
        for payment in Payment.search(domain):
            payment_amount = payment.amount
            payment_date = payment.date
            group = payment.group if payment.group else payment
            groups['amount'] += payment_amount

            # Group by group and date
            key = (group, payment_date)
            if (key in groups['groups']
                    and groups['groups'][key]['amount'] < abs(amount)):
                groups['groups'][key]['amount'] += payment_amount
                groups['groups'][key]['payments'].append(payment)
            else:
                groups['groups'][key] = {
                    'amount': payment_amount,
                    'payments': [payment],
                    }

            if payment_amount == abs(amount):
                continue

            # Group by date
            key = (None, payment_date)
            if (key in groups['groups']
                    and groups['groups'][key]['amount'] < abs(amount)):
                groups['groups'][key]['amount'] += payment_amount
                groups['groups'][key]['payments'].append(payment)
            else:
                groups['groups'][key] = {
                    'amount': payment_amount,
                    'payments': [payment],
                    }

            # Some Bancs group payments from different, but consecutive dates.
            # Normally the day before the payment value date + the date.
            delta = timedelta(days=1)
            origin_date = self.date
            if (payment_date == origin_date
                    or payment_date + delta == origin_date):
                key = (None, origin_date, delta)
                if (key in groups['groups']
                        and groups['groups'][key]['amount'] < abs(amount)):
                    groups['groups'][key]['amount'] += payment_amount
                    groups['groups'][key]['payments'].append(payment)
                else:
                    groups['groups'][key] = {
                        'amount': payment_amount,
                        'payments': [payment],
                        }

        name = gettext('account_statement_enable_banking.msg_payments')
        used_payments = []
        if groups['amount'] == abs(amount) and len(groups['groups']) > 1:
            move_lines.extend([p.line for v in groups['groups'].values()
                for p in v['payments']])
            parent, to_create = self.create_payment_suggested_line(move_lines,
                amount, name=name, similarity=acceptable)
            suggesteds.extend(to_create)
        elif groups['amount'] != _ZERO:
            lines = []
            for key, vals in groups['groups'].items():
                group = key[0]
                date = key[1]
                if vals['payments'] in used_payments:
                    continue
                if vals['amount'] == abs(amount):
                    similarity = self.increase_similarity_by_interval_date(
                        date, similarity=acceptable)
                    payment_lines = [x.line for x in vals['payments']]
                    lines.extend(payment_lines)
                    # Only check the party similarity if have one payment
                    if len(groups['groups']) == 1 and len(payment_lines) == 1:
                        party = vals['payments'][0].party
                        if party and parties:
                            similarity = self.increase_similarity_by_party(
                                party, parties, similarity=similarity)
                    name = group.rec_name if group else name
                    parent, to_create = self.create_payment_suggested_line(
                        payment_lines, amount, name=name,
                        similarity=similarity)
                    suggesteds.extend(to_create)
                    used_payments.append(vals['payments'])
            move_lines.extend(lines)
        return suggesteds, move_lines

    def _search_move_line_reconciliation_domain(self, exclude_ids=None,
            second_currency=None):
        domain = [
            ('move.company', '=', self.company.id),
            ('currency', '=', self.currency),
            ('move_state', '=', 'posted'),
            ('reconciliation', '=', None),
            ('account.reconcile', '=', True),
            ('invoice_payment', '=', None),
            ]
        if second_currency:
            domain.append(('second_currency', '=', second_currency))
        if exclude_ids:
            domain.append(('id', 'not in', exclude_ids))
        return domain

    def _search_suggested_reconciliation_move_line(self, amount, acceptable=0,
            parties=None, exclude=None, second_currency=None):
        """
        Search for any move line, not related to invoice or payments that the
        amount it the origin pending_amount
        """
        pool = Pool()
        MoveLine = pool.get('account.move.line')

        suggesteds = []

        # search only for the same ammount and possible party
        if not amount:
            return suggesteds

        # Prepapre the base domain
        line_ids = [x.id for x in exclude] if exclude else None
        domain = self._search_move_line_reconciliation_domain(
            exclude_ids=line_ids, second_currency=second_currency)

        min_amount_tolerance = self.statement.journal.min_amount_tolerance
        max_amount_tolerance = self.statement.journal.max_amount_tolerance

        lines_by_origin = {}
        lines_by_party = {}
        for line in MoveLine.search(domain, order=[('maturity_date', 'ASC')]):
            if second_currency and second_currency != self.currency:
                move_amount = line.amount_second_currency
            else:
                move_amount = line.debit - line.credit
            if (move_amount == amount
                    or (move_amount <= amount + max_amount_tolerance
                        and move_amount >= amount - min_amount_tolerance)):
                similarity = self.increase_similarity_by_interval_date(
                    line.maturity_date, similarity=acceptable)
                party = line.party
                if party and parties:
                    similarity = self.increase_similarity_by_party(party,
                        parties, similarity=similarity)
                name = None
                if line.origin:
                    name = line.origin.rec_name
                elif line.move_origin:
                    name = line.move_origin.rec_name
                elif party:
                    name = line.party.rec_name
                parent, to_create = self.create_move_suggested_line([line],
                    amount, name=name, similarity=similarity)
                suggesteds.extend(to_create)
            else:
                party = line.party
                origin = line.move_origin
                if origin:
                    if origin in lines_by_origin:
                        lines_by_origin[origin]['amount'] += move_amount
                        lines_by_origin[origin]['lines'].append(line)
                    else:
                        lines_by_origin[origin] = {
                            'amount': move_amount,
                            'lines': [line]
                                }
                elif party:
                    if party in lines_by_party:
                        lines_by_party[party]['amount'] += move_amount
                        lines_by_party[party]['lines'].append(line)
                    else:
                        lines_by_party[party] = {
                            'amount': move_amount,
                            'lines': [line]
                                }
        # Check if there are more than one move from the same origin
        # that sum the pending_amount
        for origin, values in lines_by_origin.items():
            if values['amount'] == amount:
                similarity = acceptable
                line_parties = [x.party for x in values['lines']]
                party = (line_parties[0]
                    if line_parties.count(line_parties[0]) == len(line_parties)
                    else None)
                dates = [x.maturity_date for x in values['lines']]
                date = (dates[0] if dates.count(dates[0]) == len(dates)
                    else None)
                if date:
                    similarity = self.increase_similarity_by_interval_date(
                        date, similarity=similarity)
                if party and parties:
                    similarity = self.increase_similarity_by_party(party,
                        parties, similarity=similarity)
                _, to_create = self.create_move_suggested_line(
                    values['lines'], amount, name=origin.rec_name,
                    similarity=similarity)
                suggesteds.extend(to_create)

        # Check if there are more than one move from the same party
        # that sum the pending_amount
        for party, values in lines_by_party.items():
            if values['amount'] == amount:
                similarity = acceptable
                dates = [x.maturity_date for x in values['lines']]
                date = (dates[0] if dates.count(dates[0]) == len(dates)
                    else None)
                if date:
                    similarity = self.increase_similarity_by_interval_date(
                        date, similarity=similarity)
                if parties:
                    similarity = self.increase_similarity_by_party(party,
                        parties, similarity=similarity)
                name = party.rec_name
                _, to_create = self.create_move_suggested_line(
                    values['lines'], amount, name=name, similarity=similarity)
                suggesteds.extend(to_create)
        return suggesteds

    def _search_suggested_reconciliation_simlarity(self, amount, company=None,
            information=None, threshold=0):
        """
        Search for old origins lines. Reproducing the same line/s created.
        """
        pool = Pool()
        Statement = pool.get('account.statement')
        Origin = pool.get('account.statement.origin')
        Line = pool.get('account.statement.line')
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        statement_table = Statement.__table__()
        origin_table = Origin.__table__()
        line_table = Line.__table__()
        cursor = Transaction().connection.cursor()

        if not company:
            company = Transaction().context.get('company')

        suggesteds = []

        if not amount or not information or not company:
            return suggesteds

        similarity_column = Similarity(JsonbExtractPathText(
                origin_table.information, 'remittance_information'),
            information)
        query = origin_table.join(line_table,
            condition=origin_table.id == line_table.origin).join(
                statement_table,
                condition=origin_table.statement == statement_table.id).select(
            origin_table.id, similarity_column,
            where=((similarity_column >= threshold/10)
                & (statement_table.company == company.id)
                & (origin_table.state == 'posted')
                & (line_table.related_to == None))
                )
        cursor.execute(*query)
        name = gettext('account_statement_enable_banking.msg_similarity')
        last_similarity = 0
        for origins in cursor.fetchall():
            origin, = Origin.browse([origins[0]])
            acceptable = int(origins[1] * 10)
            if acceptable == last_similarity:
                continue
            suggests = []
            for line in origin.lines:
                values = self._get_suggested_values(None, name, line,
                    line.amount, None, acceptable)
                suggests.append(values)
            if len(suggests) == 1:
                suggests[0]['amount'] = amount
            elif len(suggests) > 1:
                parent = SuggestedLine()
                parent.origin = self
                parent.name = name
                parent.amount = amount
                parent.state = 'proposed'
                parent.similarity = acceptable
                parent.save()
                for suggest in suggests:
                    suggest['parent'] = parent
                    suggest['name'] = ''
            suggesteds.extend(suggests)
            last_similarity = acceptable
        return suggesteds

    @classmethod
    def _search_reconciliation(cls, origins):
        pool = Pool()
        SuggestedLine = pool.get('account.statement.origin.suggested.line')
        StatementLine = pool.get('account.statement.line')
        try:
            Clearing = pool.get('account.payment.clearing')
        except:
            Clearing = None

        if not origins:
            return

        # Before a new search remove all suggested lines, but control if any
        # of them are related to a statement line.
        suggests = SuggestedLine.search([
                ('origin', 'in', origins),
                ])
        if suggests:
            lines = StatementLine.search([
                    ('suggested_line', 'in', [x.id for x in suggests])
                ])
            if lines:
                origins_name = ", ".join([x.origin.rec_name
                        for x in lines if x.origin])
                raise AccessError(
                    gettext('account_statement_enable_banking.'
                        'msg_suggested_line_related_to_statement_line',
                        origins_name=origins_name))
            SuggestedLine.delete(suggests)

        suggesteds_to_create = []
        for origin in origins:
            pending_amount = origin.pending_amount
            if pending_amount == _ZERO:
                return

            information = origin.remittance_information
            similarity_parties = origin.similarity_parties(information)
            threshold = origin.similarity_threshold
            acceptable = origin.acceptable_similarity
            groups = []
            move_lines = []

            # If account_pauyment_clearing modules is isntalled search first
            # for the groups or payments
            if Clearing:
                # Search by possible payment groups with clearing journal
                # deffined
                suggest_lines, used_groups = (
                    origin.
                    _search_suggested_reconciliation_clearing_payment_group(
                        pending_amount, acceptable=acceptable))
                suggesteds_to_create.extend(suggest_lines)
                groups.extend(used_groups)

                # Search by possible payments with clearing journal deffined
                suggest_lines, used_move_lines = (
                    origin._search_suggested_reconciliation_clearing_payment(
                        pending_amount, acceptable=acceptable,
                        parties=similarity_parties, exclude=groups))
                suggesteds_to_create.extend(suggest_lines)
                move_lines.extend(used_move_lines)

            # Search by possible part or all of payment groups
            suggest_lines, used_move_lines = (
                origin._search_suggested_reconciliation_payment(pending_amount,
                    acceptable=acceptable, parties=similarity_parties,
                    exclude_groups=groups, exclude_lines=move_lines))
            suggesteds_to_create.extend(suggest_lines)
            move_lines.extend(used_move_lines)

            # Search by move_line, with or without origin and party
            suggest_lines = (
                origin._search_suggested_reconciliation_move_line(
                    pending_amount, acceptable=acceptable,
                    parties=similarity_parties, exclude=move_lines))
            suggesteds_to_create.extend(suggest_lines)

            # Search by second currency
            if origin.second_currency and origin.amount_second_currency != 0:
                suggest_lines = (
                    origin._search_suggested_reconciliation_move_line(
                        origin.amount_second_currency, acceptable=acceptable,
                        parties=similarity_parties,
                        second_currency=origin.second_currency))
                suggesteds_to_create.extend(suggest_lines)

            # Search by simlarity, using the PostreSQL Trigram
            suggesteds_to_create.extend(
                origin._search_suggested_reconciliation_simlarity(
                        pending_amount, company=origin.company,
                        information=information, threshold=threshold))

        def remove_duplicate_suggestions(suggesteds):
            seen = set()
            result = []
            keys = ['name', 'parent', 'origin', 'party', 'date', 'related_to',
                'account', 'amount', 'second_currency', 'similarity',
                'amount_second_currency', 'state']
            for suggest in suggesteds:
                # Create an identifier based in the main keys.
                identifier = tuple(suggest[key] for key in keys if key in suggest)
                if identifier not in seen:
                    result.append(suggest)
                    seen.add(identifier)
            return result

        suggesteds_use = []
        if suggesteds_to_create:
            suggesteds_to_create = remove_duplicate_suggestions(
                suggesteds_to_create)
            SuggestedLine.create(suggesteds_to_create)
            for origin in origins:
                suggested_use = None
                before_similarity = 0.0
                for suggest in SuggestedLine.search([
                        ('origin', '=', origin),
                        ('parent', '=', None),
                        ('similarity', '>=', origin.acceptable_similarity)
                        ]):
                    if suggest.similarity == before_similarity:
                        suggested_use = None
                        break
                    elif suggest.similarity < before_similarity:
                        break
                    suggested_use = suggest
                    before_similarity = suggest.similarity
                if suggested_use:
                    suggesteds_use.append(suggested_use)
        if suggesteds_use:
            SuggestedLine.use(suggesteds_use)

    @classmethod
    def _get_statement_line(cls, origin, related):
        pool = Pool()
        StatementLine = pool.get('account.statement.line')
        Invoice = pool.get('account.invoice')
        Date = pool.get('ir.date')
        Currency = pool.get('currency.currency')

        if isinstance(related, Invoice):
            sign = -1 if related.type == 'in' else 1
            amount = sign * related.amount_to_pay
            second_currency = related.currency
            if origin.second_currency:
                second_currency_date = related.currency_date or Date.today()
                with Transaction().set_context(date=second_currency_date):
                    amount_to_pay = Currency.compute(second_currency,
                        related.amount_to_pay, origin.company.currency,
                        round=True)
                amount_second_currency = sign * amount_to_pay
            else:
                amount_second_currency = sign * related.amount_to_pay
        else:
            amount=related.amount
            second_currency = related.second_currency
            amount_second_currency = related.amount_second_currency

        line = StatementLine()
        line.origin = origin
        line.statement = origin.statement
        line.suggested_line = None
        line.related_to = related
        line.party = related.party
        line.account = related.account
        line.amount = amount
        if origin.second_currency:
            line.second_currency = second_currency
            line.amount_second_currency = amount_second_currency
        line.date = origin.date
        line.maturity_date = None
        line.description = origin.remittance_information
        return line

    @classmethod
    @ModelView.button
    def search_suggestions(cls, origins):
        cls._search_reconciliation(origins)

    @classmethod
    def delete(cls, origins):
        for origin in origins:
            if origin.state not in {'cancelled', 'registered'}:
                raise AccessError(
                    gettext(
                        'account_statement.'
                        'msg_statement_origin_delete_cancel_draft',
                        origin=origin.rec_name,
                        sale=origin.statement.rec_name))
        # Use __func__ to directly access ModelSQL's delete method and
        # pass it the right class
        ModelSQL.delete.__func__(cls, origins)

    @classmethod
    def copy(cls, origins, default=None):
        default = default.copy() if default is not None else {}
        default.setdefault('entry_reference', None)
        default.setdefault('suggested_lines', None)
        default.setdefault('balance', None)
        default.setdefault('state', 'registered')
        return super().copy(origins, default=default)

    @classmethod
    @ModelView.button_action(
            'account_statement_enable_banking.wizard_multiple_invoices')
    def multiple_invoices(cls, origins):
        pass

    @classmethod
    @ModelView.button_action(
            'account_statement_enable_banking.wizard_multiple_move_lines')
    def multiple_move_lines(cls, origins):
        pass

    @classmethod
    @ModelView.button
    @Workflow.transition('registered')
    def register(cls, origins):
        pool = Pool()
        Statement = pool.get('account.statement')

        # Control the statement state.
        # Statement is a required field in Origin class
        statements = [x.statement for x in origins
            if x.statement.state == 'posted']
        if statements:
            Statement.write(statements, {'state': 'draft'})

    @classmethod
    @ModelView.button
    @Workflow.transition('posted')
    def post(cls, origins):
        pool = Pool()
        Statement = pool.get('account.statement')
        StatementLine = pool.get('account.statement.line')

        cls.validate_origin(origins)
        cls.create_moves(origins)

        lines = [x for o in origins for x in o.lines]
        # It's an awful hack to set the state, but it's needed to ensure the
        # Error of statement state in Move.post is not applied when try to
        # concile and individual origin. For this, need the state == 'posted'.
        statements = [o.statement for o in origins]
        statement_state = []
        for origin in origins:
            statement_state.append([origin.statement])
            statement_state.append({
                    'state': origin.statement.state,
                    })
        if statements:
            Statement.write(statements, {'state': 'posted'})
        StatementLine.post_move(lines)
        if statement_state:
            Statement.write(*statement_state)
        # End awful hack

        # Check if the statement of the origin has all the origins posted, so
        # the statement could be posted too.
        statements_to_post = []
        for statement in statements:
            if all(x.state == 'posted'
                    for x in statement.origins if x not in origins):
                getattr(statement, 'validate_%s' % statement.validation)()
                statements_to_post.append(statement)
        if statements_to_post:
            Statement.write(statements_to_post, {'state': 'posted'})

    @classmethod
    @ModelView.button
    @Workflow.transition('cancelled')
    def cancel(cls, origins):
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        StatementLine = pool.get('account.statement.line')
        Warning = pool.get('res.user.warning')

        lines = [x for origin in origins for x in origin.lines]
        moves = dict((x.move, x.origin) for x in lines if x.move)
        if moves:
            warning_key = Warning.format('cancel_origin_line_with_move',
                list(moves.keys()))
            if Warning.check(warning_key):
                raise StatementValidateWarning(warning_key,
                    gettext('account_statement_enable_banking.'
                        'msg_cancel_origin_line_with_move',
                        moves=", ".join(m.rec_name for m in moves)))
            StatementLine.cancel_move(moves.keys())
            with Transaction().set_context(
                    from_account_statement_origin=True):
                to_write = []
                for move, origin in moves.items():
                    move_lines = []
                    for line in move.lines:
                        if line.origin in origin.lines:
                            move_lines.append(line)
                    to_write.extend((move_lines, {'origin': origin}))
                if to_write:
                    MoveLine.write(*to_write)
            StatementLine.write(lines, {'move': None})


class OriginSuggestedLine(Workflow, ModelSQL, ModelView, tree()):
    'Account Statement Origin Suggested Line'
    __name__ = 'account.statement.origin.suggested.line'

    name = fields.Char('Name')
    parent = fields.Many2One('account.statement.origin.suggested.line',
        "Parent")
    childs = fields.One2Many('account.statement.origin.suggested.line',
        'parent', 'Children')
    origin = fields.Many2One('account.statement.origin', 'Origin',
        required=True, ondelete='CASCADE')
    company = fields.Function(fields.Many2One('company.company', "Company"),
        'on_change_with_company', searcher='search_company')
    company_currency = fields.Function(
        fields.Many2One('currency.currency', "Company Currency"),
        'on_change_with_company_currency')
    party = fields.Many2One('party.party', "Party",
        context={
            'company': Eval('company', -1),
            },
        depends={'company'})
    date = fields.Date("Date")
    account = fields.Many2One('account.account', "Account",
        domain=[
            ('company', '=', Eval('company', 0)),
            ('type', '!=', None),
            ('closed', '!=', True),
            ])
    amount = Monetary("Amount", currency='currency', digits='currency',
        required=True)
    currency = fields.Function(fields.Many2One('currency.currency',
        "Currency"), 'on_change_with_currency')
    amount_second_currency = Monetary("Amount Second Currency",
        currency='second_currency', digits='second_currency',
        states={
            'required': Bool(Eval('second_currency')),
            })
    second_currency = fields.Many2One(
        'currency.currency', "Second Currency",
        domain=[
            ('id', '!=', Eval('currency', -1)),
            If(Eval('currency', -1) != Eval('company_currency', -1),
                ('id', '=', Eval('company_currency', -1)),
                ()),
            ])
    related_to = fields.Reference(
        "Related To", 'get_relations',
        domain={
            'account.invoice': [
                ('company', '=', Eval('company', -1)),
                If(Bool(Eval('second_currency')),
                    ('currency', '=', Eval('second_currency', -1)),
                    ('currency', '=', Eval('currency', -1))
                    ),
                If(Bool(Eval('party')),
                    ['OR',
                        ('party', '=', Eval('party', -1)),
                        ('alternative_payees', '=', Eval('party', -1)),
                        ],
                    []),
                If(Bool(Eval('account')),
                    ('account', '=', Eval('account')),
                    ()),
                ('state', '=', 'posted'),
                ],
            'account.payment': [
                ('company', '=', Eval('company', -1)),
                ('currency', '=', Eval('currency', -1)),
                ],
            'account.payment.group': [
                ('company', '=', Eval('company', -1)),
                If(Bool(Eval('second_currency')),
                    ('currency', '=', Eval('second_currency', -1)),
                    ('currency', '=', Eval('currency', -1))),
                ],
            'account.move.line': [
                ('company', '=', Eval('company', -1)),
                ('currency', '=', Eval('currency', -1)),
                If(Bool(Eval('party')),
                    ('party', '=', Eval('party')),
                    ()),
                If(Bool(Eval('account')),
                    ('account', '=', Eval('account')),
                    ()),
                ('account.reconcile', '=', True),
                ('state', '=', 'valid'),
                ('reconciliation', '=', None),
                ('invoice_payment', '=', None),
                ],
            })
    similarity = fields.Integer('Similarity',
        help=('The thershold used for similarity function in origin lines '
            'search'))
    state = fields.Selection([
            ('proposed', "Proposed"),
            ('used', "Used"),
            ], "State", readonly=True, sort=False)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order.insert(0, ('similarity', 'DESC'))
        cls._transitions |= set((
                ('proposed', 'used'),
                ('used', 'proposed'),
                ))
        cls._buttons.update({
                'propose': {
                    'invisible': Eval('state') == 'proposed',
                    'depends': ['state'],
                    },
                'use': {
                    'invisible': Eval('state') == 'used',
                    'depends': ['state'],
                    },
                })

    @staticmethod
    def default_state():
        return 'proposed'

    @fields.depends('origin', '_parent_origin.company')
    def on_change_with_company(self, name=None):
        return self.origin.company if self.origin else None

    @fields.depends('origin', '_parent_origin.company')
    def on_change_with_company_currency(self, name=None):
        return (self.origin.company.currency
            if self.origin and self.origin.company else None)

    @classmethod
    def search_company(cls, name, clause):
        return [('origin.' + clause[0],) + tuple(clause[1:])]

    @classmethod
    def _get_relations(cls):
        "Return a list of Model names for related_to Reference"
        return [
            'account.invoice',
            'account.payment',
            'account.payment.group',
            'account.move.line']

    @classmethod
    def get_relations(cls):
        Model = Pool().get('ir.model')
        get_name = Model.get_name
        models = cls._get_relations()
        return [(None, '')] + [(m, get_name(m)) for m in models]

    @fields.depends('origin', '_parent_origin.statement')
    def on_change_with_currency(self, name=None):
        if self.origin and self.origin.statement:
            return self.origin.statement.currency

    @classmethod
    @ModelView.button
    @Workflow.transition('proposed')
    def propose(cls, recomended):
        pass

    @classmethod
    def get_suggested_values(cls, child, description=None):
        return {
            'origin': child.origin,
            'statement': child.origin.statement,
            'suggested_line': child,
            'related_to': child.related_to,
            'party': child.party,
            'account': child.account,
            'amount': child.amount,
            'second_currency': child.second_currency,
            'amount_second_currency': child.amount_second_currency,
            'date': child.origin.date,
            'description': description or '',
            }

    @classmethod
    @ModelView.button
    @Workflow.transition('used')
    def use(cls, recomended):
        pool = Pool()
        StatementLine = pool.get('account.statement.line')

        to_create = []
        for recomend in recomended:
            childs = recomend.childs if recomend.childs else [recomend]
            for child in childs:
                if child.state == 'used':
                    continue
                description = child.origin.remittance_information
                values = cls.get_suggested_values(child, description)
                to_create.append(values)
            if len(childs) > 1:
                cls.write(list(childs), {'state': 'used'})
        StatementLine.create(to_create)


class AddMultipleInvoices(Wizard):
    'Add Multiple Invoices'
    __name__ = 'account.statement.origin.multiple.invoices'
    start = StateView('account.statement.origin.multiple.invoices.start',
        'account_statement_enable_banking.'
        'statement_multiple_invoices_start_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('OK', 'create_lines', 'tryton-ok', True),
            ])
    create_lines = StateTransition()

    def default_start(self, fields):
        return {
            'company': self.record.company.id,
            'currency': (self.record.second_currency.id
                if self.record.second_currency else self.record.currency.id),
            }

    def transition_create_lines(self):
        pool = Pool()
        StatementOrigin = pool.get('account.statement.origin')
        StatementLine = pool.get('account.statement.line')

        lines = []
        for invoice in self.start.invoices:
            line = StatementOrigin._get_statement_line(self.record, invoice)
            lines.append(line)
        if lines:
            StatementLine.save(lines)
        return 'end'


class AddMultipleInvoicesStart(ModelView):
    'Add Multiple Invoices Start'
    __name__ = 'account.statement.origin.multiple.invoices.start'

    company = fields.Many2One('company.company', "Company")
    currency = fields.Many2One('currency.currency', "Currency")
    invoices = fields.Many2Many('account.invoice', None, None,
        "Invoices",
        domain=[
            ('company', '=', Eval('company', -1)),
            ('currency', '=', Eval('currency', -1)),
            ('state', '=', 'posted'),
            ], required=True)


class AddMultipleMoveLines(Wizard):
    'Add Multiple Move Lines'
    __name__ = 'account.statement.origin.multiple.move_lines'
    start = StateView('account.statement.origin.multiple.move_lines.start',
        'account_statement_enable_banking.'
        'statement_multiple_move_lines_start_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('OK', 'create_lines', 'tryton-ok', True),
            ])
    create_lines = StateTransition()

    def default_start(self, fields):
        return {
            'company': self.record.company.id,
            'currency': self.record.currency.id,
            'second_currency': (self.record.second_currency.id
                if self.record.second_currency else None),
            }

    def transition_create_lines(self):
        pool = Pool()
        StatementOrigin = pool.get('account.statement.origin')
        StatementLine = pool.get('account.statement.line')

        lines = []
        for move_line in self.start.move_lines:
            line = StatementOrigin._get_statement_line(self.record, move_line)
            lines.append(line)
        if lines:
            StatementLine.save(lines)
        return 'end'


class AddMultipleMoveLinesStart(ModelView):
    'Add Multiple Move Lines Start'
    __name__ = 'account.statement.origin.multiple.move_lines.start'

    company = fields.Many2One('company.company', "Company")
    currency = fields.Many2One('currency.currency', "Currency")
    second_currency = fields.Many2One('currency.currency', "Second Currency")
    move_lines = fields.Many2Many('account.move.line', None, None,
        "Move Lines",
        domain=[
            ('company', '=', Eval('company', -1)),
            If(Eval('second_currency'),
                ('second_currency', '=', Eval('second_currency', -1)),
                ('currency', '=', Eval('currency', -1))
               ),
            ('account.reconcile', '=', True),
            ('state', '=', 'valid'),
            ('move_state', '=', 'posted'),
            ('reconciliation', '=', None),
            ('invoice_payment', '=', None),
            ], required=True)


class SynchronizeStatementEnableBankingStart(ModelView):
    "Synchronize Statement Enable Banking Start"
    __name__ = 'enable_banking.synchronize_statement.start'


class SynchronizeStatementEnableBanking(Wizard):
    "Synchronize Statement Enable Banking"
    __name__ = 'enable_banking.synchronize_statement'

    start = StateView('enable_banking.synchronize_statement.start',
        'account_statement_enable_banking.'
        'enable_banking_synchronize_statement_start_form',
        [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('OK', 'check_session', 'tryton-ok', default=True),
        ])
    check_session = StateTransition()
    create_session = StateAction(
        'account_statement_enable_banking.url_session')
    sync_statements = StateTransition()

    def transition_check_session(self):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        EBSession = pool.get('enable_banking.session')
        base_headers = get_base_header()

        active_id = Transaction().context.get('active_id', None)
        journal = Journal(active_id) if active_id else None
        if not journal or not journal.bank_account:
            raise AccessError(gettext(
                    'account_statement_enable_banking.msg_no_bank_account'))

        if journal.enable_banking_session:
            # We need to check the date and if we have the field session, if
            # not the session was not created correctly and need to be deleted
            eb_session = journal.enable_banking_session
            if (eb_session.session and eb_session.valid_until and
                    eb_session.valid_until >= datetime.now()):
                session = eval(eb_session.session)
                r = requests.get(
                    f"{config.get('enable_banking', 'api_origin')}"
                    f"/sessions/{session['session_id']}",
                    headers=base_headers)
                if r.status_code == 200:
                    session = r.json()
                    if session['status'] == 'AUTHORIZED':
                        return 'sync_statements'
            EBSession.delete([eb_session])
        return 'create_session'

    def do_create_session(self, action):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        EBSession = pool.get('enable_banking.session')

        journal = Journal(Transaction().context['active_id'])
        bank_name = journal.bank_account.bank.party.name.lower()
        bic = (journal.bank_account.bank.bic or '').lower()
        if journal.bank_account.bank.party.addresses:
            country = journal.bank_account.bank.party.addresses[0].country.code
        else:
            raise AccessError(gettext('account_statement_enable_banking.'
                    'msg_no_country'))

        # We fill the aspsp name and country using the bank account
        base_headers = get_base_header()
        r = requests.get(
            f"{config.get('enable_banking', 'api_origin')}/aspsps",
            headers=base_headers)
        response = r.json()
        aspsp_found = False
        for aspsp in response.get("aspsps", []):
            if aspsp["country"] != country:
                continue
            if (aspsp["name"].lower() == bank_name
                    or aspsp.get("bic", " ").lower() == bic):
                journal.aspsp_name = aspsp["name"]
                journal.aspsp_country = aspsp["country"]
                aspsp_found = True
                break

        if not aspsp_found:
            message = response.get('message', '')
            raise AccessError(
                gettext('account_statement_enable_banking.msg_aspsp_not_found',
                    bank=journal.aspsp_name,
                    country_code=journal.aspsp_country,
                    message=message))

        eb_session = EBSession()
        eb_session.company = journal.company
        eb_session.aspsp_name = journal.aspsp_name
        eb_session.aspsp_country = journal.aspsp_country
        eb_session.bank = journal.bank_account.bank
        eb_session.session_id = token_hex(16)
        eb_session.valid_until = (
            datetime.now() + journal.enable_banking_session_valid_days)
        EBSession.save([eb_session])
        base_headers = get_base_header()
        body = {
            'access': {'valid_until': (datetime.now(UTC)
                    + journal.enable_banking_session_valid_days).isoformat()},
            'aspsp': {
                'name': journal.aspsp_name,
                'country': journal.aspsp_country},
            'state': eb_session.session_id,
            'redirect_url': config.get('enable_banking', 'redirecturl'),
            'psu_type': 'personal',
        }

        r = requests.post(f"{config.get('enable_banking', 'api_origin')}/auth",
            json=body, headers=base_headers)

        if r.status_code == 200:
            action['url'] = r.json()['url']
        else:
            raise AccessError(
                gettext('account_statement_enable_banking.'
                    'msg_error_create_session',
                    error_code=r.status_code,
                    error_message=r.text))
        journal.enable_banking_session = eb_session
        journal.save()
        return action, {}

    def transition_sync_statements(self):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        journal = Journal(Transaction().context['active_id'])
        if not journal.enable_banking_session:
            raise AccessError(
                gettext('account_statement_enable_banking.msg_no_session'))
        journal.synchronize_statements_enable_banking()
        return 'end'


class OriginSynchronizeStatementEnableBankingAsk(ModelView):
    "Statement Origin or Synchronize Statement Enable Banking Ask"
    __name__ = 'enable_banking.origin_synchronize_statement.ask'

    journals = fields.Many2Many('account.statement.journal', None, None,
        'Journals', readonly=True, states={
            'invisible': True,
            })


class OriginSynchronizeStatementEnableBanking(Wizard):
    "Statement Origin or Synchronize Statement Enable Banking"
    __name__ = 'enable_banking.origin_synchronize_statement'

    start = StateTransition()
    ask = StateView('enable_banking.origin_synchronize_statement.ask',
        'account_statement_enable_banking.'
        'origin_synchronize_statement_ask_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Origin', 'origin', 'tryton-cancel'),
            Button('Journal', 'journal', 'tryton-ok', default=True),
            ])
    origin = StateAction('account_statement_enable_banking.'
        'act_statement_origin_form')
    journal = StateAction('account_statement.act_statement_journal_form')

    def get_journals_unsynchonized(self):
        pool = Pool()
        Journal = pool.get('account.statement.journal')

        journal_unsynchronized = []
        company_id = Transaction().context.get('company')
        if not company_id:
            return []
        domain = [
            ('company.id', '=', company_id),
            ('synchronize_journal', '=', True)
            ]
        for journal in Journal.search(domain):
            if (journal.enable_banking_session is None
                    or (journal.enable_banking_session
                        and (journal.enable_banking_session.session is None
                            or (journal.enable_banking_session.valid_until
                                and journal.enable_banking_session.valid_until
                                < datetime.now())))):
                journal_unsynchronized.append(journal)
        return journal_unsynchronized

    def transition_start(self):
        if self.get_journals_unsynchonized():
            return 'ask'
        return 'origin'

    def default_ask(self, fields):
        journal_unsynchronized = self.get_journals_unsynchonized()
        return {
            'journals': [x.id for x in journal_unsynchronized],
            }

    def do_origin(self, action):
        return action, {}

    def do_journal(self, action):
        journal_ids = [x.id for x in self.ask.journals]
        action['pyson_domain'] = PYSONEncoder().encode([
            ('id', 'in', journal_ids),
            ])
        return action, {}
