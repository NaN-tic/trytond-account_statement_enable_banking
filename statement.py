# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import re
import difflib
import json
import hashlib
import math
import requests
import functools
from unidecode import unidecode
from datetime import datetime, UTC, timedelta
from decimal import Decimal
from secrets import token_hex
from itertools import chain, combinations, groupby
from sql.conditionals import Greatest
from sql.functions import Function
from trytond.model import Workflow, ModelView, ModelSQL, fields, tree
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval, Bool, If, PYSON, PYSONEncoder
from trytond.rpc import RPC
from trytond.wizard import (
    Button, StateAction, StateTransition, StateView, Wizard)
from trytond.transaction import Transaction
from .common import get_base_header, URL, REDIRECT_URL
from trytond.i18n import gettext
from trytond.exceptions import UserWarning
from trytond.model.exceptions import AccessError
from trytond.modules.account_statement.exceptions import (
    StatementValidateError, StatementValidateWarning)
from trytond.modules.currency.fields import Monetary
from trytond.modules.account_statement.statement import Unequal
from trytond import backend
from trytond.config import config


ZERO = Decimal(0)
PRODUCTION = config.get('database', 'production', default=False)
PARTY_SIMILARITY_THRESHOLD = config.get('enable_banking',
    'party_similarity_threshold', default=0.13)

@functools.cache
def gaussian_score(x, mean, stddev):
    'Return 1 at x==mean and decay like a Gaussian as |x-mean| increases.'
    if stddev <= 0:
        raise ValueError("stddev must be positive")
    return math.exp(-((x - mean) ** 2) / (2.0 * (stddev ** 2)))

@functools.cache
def candidate_size(k, target_combinations, max_n=10000):
    low = k
    high = 1000000
    while low < high:
        mid = (low + high) // 2
        if math.comb(mid, k) < target_combinations:
            low = mid + 1
        else:
            high = mid
    n = low
    return min(n, max_n)

def clean_string(s):
    s = unidecode(s.lower())
    # remove punctuation, keep letters/digits/space
    s = s.replace('/', ' ').replace('.', ' ').replace('-', ' ').replace(',', ' ')
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

@functools.cache
def longest_common_substring(a, b):
    a = clean_string(a)
    b = clean_string(b)
    matcher = difflib.SequenceMatcher(None, a, b)
    match = matcher.find_longest_match(0, len(a), 0, len(b))
    return match.size

def compare_party(party_name, text):
    if not party_name:
        return 0
    party_name = clean_string(party_name)
    text = clean_string(text)
    names = [party_name]
    if ' ' in party_name:
        # Change the last word for the first one
        start, _, end = party_name.rpartition(' ')
        names.append(end + ' ' + start)
    percent = 0
    for name in names:
        length = longest_common_substring(name, text)
        percent = max(length / len(name), percent)
    return int(round(percent * 100))


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
                    'invisible': Eval('state') != 'draft',
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
                    ('date', line.origin and line.origin.date or line.date),
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


class Line(metaclass=PoolMeta):
    __name__ = 'account.statement.line'

    maturity_date = fields.Date("Maturity Date",
        states={
            'readonly': ((Eval('origin_state') != 'registered')
                | Bool(Eval('related_to'))),
            },
        depends=['related_to'],
        help="Set a date to make the line payable or receivable.")
    suggested_line = fields.Many2One('account.statement.origin.suggested.line',
        'Suggested Line',
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
    account_required = fields.Function(fields.Boolean('Account Required'),
        'on_change_with_account_required')

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
                If(Eval('company_currency') == Eval('currency'),
                    ('currency', '=', Eval('currency', -1)),
                    ('second_currency', '=', Eval('currency', -1))
                    )),
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
        _readonly = ~Eval('statement_state', '').in_(['draft', 'registered'])
        cls.statement.states['readonly'] = _readonly
        cls.number.states['readonly'] = _readonly
        cls.date.states['readonly'] = _readonly | Bool(Eval('origin', 0))
        cls.amount.states['readonly'] = _readonly
        cls.amount_second_currency.states['readonly'] = _readonly
        cls.second_currency.states['readonly'] = _readonly
        cls.party.states['readonly'] = _readonly
        cls.party.states['required'] = (Eval('party_required', False)
            & (Eval('statement_state').in_(['draft', 'registered']))
            )
        cls.account.required = None
        cls.account.states['readonly'] = _readonly
        cls.account.states['required'] = Eval('account_required', True)
        cls.description.states['readonly'] = _readonly
        cls.related_to.states['readonly'] = _readonly
        cls._buttons.update({
                'add_pending': {
                    'invisible': Eval('origin_state') != 'registered',
                    'depends': ['origin_state'],
                    },
                })

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

    @fields.depends(methods=['payment_group'])
    def on_change_with_account_required(self, name=None):
        if (self.payment_group
                and not self.payment_group.journal.clearing_account):
            return False
        return True

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
        pool = Pool()
        Invoice = pool.get('account.invoice')

        amount_to_pay = None
        # control the possibilty to use the move from invoice
        invoice = self.invoice or self.move_line_invoice or None
        if invoice:
            with Transaction().set_context(with_payment=False):
                invoice, = Invoice.browse([invoice])
            sign = -1 if invoice.type == 'in' else 1
            if invoice.currency == self.currency:
                # If we are in the case that need control a refund invoice,
                # need to get the total amount of the invoice.
                amount_to_pay = sign * (invoice.total_amount
                    if self.show_paid_invoices and invoice.state == 'paid'
                    else invoice.amount_to_pay)
            else:
                amount = ZERO
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
                        else ZERO)
                amount_to_pay = amount
        if self.show_paid_invoices and amount_to_pay:
            amount_to_pay = -1 * amount_to_pay
        return amount_to_pay

    @fields.depends('show_paid_invoices')
    def on_change_party(self):
        if not self.show_paid_invoices:
            super().on_change_party()

    @fields.depends('amount', 'account', 'origin', '_parent_origin.id',
        methods=['invoice', 'move_line', 'invoice_amount_to_pay'])
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
            # By default, super().on_change_amount() may set the account
            # automatically based on the sign of the amount, but we don't want
            # that
            account = self.account
            super().on_change_amount()
            if self.origin:
                self.account = account

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
            self.maturity_date = self.move_line.maturity_date
        if self.invoice:
            lines_to_pay = [l for l in self.invoice.lines_to_pay
                if l.maturity_date and l.reconciliation is None]
            oldest_line = (min(lines_to_pay,
                    key=lambda line: line.maturity_date)
                if lines_to_pay else None)
            if oldest_line:
                self.maturity_date = oldest_line.maturity_date
        related_to = getattr(self, 'related_to', None)
        if self.show_paid_invoices and not isinstance(related_to, Invoice):
            self.show_paid_invoices = False

        if not self.description and self.origin and self.origin.information:
            self.description = self.origin.remittance_information

        # TODO: Control when the currency is different
        payment_groups = set()
        payments = set()
        move_lines = set()
        move_lines_second_currency = set()
        invoice_id2amount_to_pay = {}

        if self.invoice and self.invoice.id not in invoice_id2amount_to_pay:
            invoice_id2amount_to_pay[self.invoice.id] = (
                self.invoice_amount_to_pay)
        if (self.payment_group
                and self.payment_group.currency == self.company.currency):
            payment_groups.add(self.payment_group)
        if self.payment and self.payment.currency == self.company.currency:
            payments.add(self.payment)
        if self.move_line and self.move_line.currency == self.company.currency:
            if self.currency == self.move_line.currency:
                move_lines.add(self.move_line)
            else:
                move_lines_second_currency.add(self.move_line)

        payment_group_id2amount = (dict((x.id, x.payment_amount)
            for x in payment_groups) if payment_groups else {})

        payment_id2amount = (dict((x.id, x.amount) for x in payments)
            if payments else {})

        move_line_id2amount = (dict((x.id, x.debit-x.credit)
            for x in move_lines) if move_lines else {})

        move_line_id2amount.update(dict((x.id, x.amount)
                for x in move_lines_second_currency)
            if move_lines_second_currency else {})

        # As a 'core' difference, the value of the line amount must be the
        # amount of the movement, invoice, group or payment. Not the line
        # amount pending. It could induce an incorrect concept
        # and misunderstunding.
        amount = None
        if self.invoice and self.invoice.id in invoice_id2amount_to_pay:
            amount = invoice_id2amount_to_pay.get(
                self.invoice.id, ZERO)
        if self.payment and self.payment.id in payment_id2amount:
            amount = payment_id2amount[self.payment.id]
        if self.move_line and self.move_line.id in move_line_id2amount:
            amount = move_line_id2amount[self.move_line.id]
        if (self.payment_group
                and self.payment_group.id in payment_group_id2amount):
            amount = payment_group_id2amount[self.payment_group.id]
        if amount is None and self.invoice:
            self.invoice = None
        if amount is None and self.payment:
            self.payment = None
        if amount is None and self.payment_group:
            self.payment_group = None
        if amount is None and self.move_line:
            self.move_line = None
        self.amount = amount

    @classmethod
    def cancel_lines(cls, lines):
        pool = Pool()
        Warning = pool.get('res.user.warning')
        Move = pool.get('account.move')
        Line = pool.get('account.move.line')
        Reconciliation = pool.get('account.move.reconciliation')
        Invoice = pool.get('account.invoice')

        if any(line.move for line in lines):
            wnames = []
            for line in lines:
                if line.move:
                    wnames.append(line)
                if len(wnames) == 5:
                    break

            warning_name = 'origin_line_with_move.' + hashlib.md5(
                ''.join([str(l.id) for l in wnames]).encode('utf-8')).hexdigest()
            names = ', '.join(l.number or l.description or str(l.id) for l in wnames)
            if len(wnames) == 5:
                names += '...'

            if Warning.check(warning_name):
                raise StatementValidateWarning(warning_name,
                    gettext('account_statement_enable_banking.'
                        'msg_origin_lines_with_move',
                        lines=names))

        moves = set()
        to_unreconcile = []
        to_unpay = []
        for line in lines:
            if not line.move:
                continue

            moves.add(line.move)
            to_unreconcile += [x.reconciliation for x in line.move.lines
                if x.reconciliation]
            # On possible related invoices, need to unlink the payment
            # lines
            to_unpay += [x for x in line.move.lines if x.invoice_payment]

        if to_unreconcile:
            to_unreconcile = Reconciliation.browse(to_unreconcile)
            Reconciliation.delete(to_unreconcile)

        if moves:
            moves = list(moves)
            Move.draft(moves)
            Move.delete(moves)

        if to_unpay:
            to_unpay = Line.browse(to_unpay)
            Invoice.remove_payment_lines(to_unpay)

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
        for values in move_lines:
            if len(values) == 3:
                move_line, statement_line, payment = values
            elif len(values) == 2:
                move_line, statement_line = values
                payment = None
            else:
                continue
            if not statement_line:
                continue
            if (statement_line.invoice and statement_line.show_paid_invoices
                    and move_line.account == statement_line.invoice.account):
                invoice = statement_line.invoice
                reconcile = [move_line]
                payment_lines = list(set(chain(
                            [x for x in invoice.payment_lines],
                            invoice.reconciliation_lines)))
                payments = []
                for line in payment_lines:
                    if line.reconciliation:
                        payments.extend([p for l in line.reconciliation.lines
                                for p in l.payments if l.id != line.id])
                        # Temporally, need to allow
                        # from_account_bank_statement_line, until all is move
                        # from the old bank_statement to the new statement.
                        with Transaction().set_context(_skip_warnings=True,
                                from_account_bank_statement_line=True):
                            Reconcile.delete([line.reconciliation])
                    reconcile.append(line)
                if payments:
                    Payment.fail(payments)
                if reconcile:
                    MoveLine.reconcile(reconcile)
                if invoice.payment_lines:
                    invoice.payment_lines = None
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
            elif statement_line.payment_group or statement_line.payment:
                line = (statement_line.payment
                    if statement_line.payment else payment)
                moveline = line.line or None
                if moveline:
                    key = (line.party, moveline.account, line.amount)
                    if key in move_to_reconcile:
                        move_to_reconcile[key].append((move_line, moveline))
                    else:
                        move_to_reconcile[key] = [(move_line, moveline)]
                with Transaction().set_context(
                        clearing_date=statement_line.date):
                    Payment.succeed(Payment.browse([line]))
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
        cls.cancel_lines(lines)
        cls.suggested_to_proposed(lines)
        for line in lines:
            if line.statement_state in {'validated', 'posted'}:
                raise AccessError(
                    gettext('account_statement_enable_banking'
                        '.msg_statement_line_delete',
                        line=line.rec_name,
                        statement=line.statement.rec_name))
        # Use __func__ to directly access ModelSQL's delete method and
        # pass it the right class
        ModelSQL.delete.__func__(cls, lines)

    @classmethod
    def delete_move(cls, lines):
        cls.cancel_lines(lines)
        super().delete_move(lines)

    def get_move_line(self):
        line = super().get_move_line()
        if self.maturity_date:
            line.maturity_date = self.maturity_date
        return line

    def get_payment_group_move_line(self):
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        Currency = Pool().get('currency.currency')

        payment_group_move_lines = []
        for payment in self.payment_group.payments:
            if not payment.amount:
                continue
            with Transaction().set_context(date=payment.date):
                amount = Currency.compute(
                    payment.currency, payment.amount, self.company_currency)
            amount *= -1 if payment.kind == 'payable' else 1
            if payment.currency != self.company_currency:
                second_currency = payment.currency
                amount_second_currency = -payment.amount
            else:
                second_currency = None
                amount_second_currency = None

            account = payment.line.account if payment.line else payment.account
            party = payment.party if account.party_required else None
            payment_group_move_lines.append((MoveLine(
                    origin=self,
                    debit=abs(amount) if amount < 0 else 0,
                    credit=abs(amount) if amount > 0 else 0,
                    account=account,
                    party=party,
                    second_currency=second_currency,
                    amount_second_currency=amount_second_currency,
                    ), payment))

        return payment_group_move_lines

    @classmethod
    def copy(cls, lines, default=None):
        default = default.copy() if default is not None else {}
        default.setdefault('maturity_date', None)
        default.setdefault('suggested_line', None)
        default.setdefault('show_paid_invoices', None)
        return super().copy(lines, default=default)

    @classmethod
    @ModelView.button
    def add_pending(cls, lines):
        Origin = Pool().get('account.statement.origin')
        if not lines:
            return
        line = lines[0]
        if isinstance(line.origin, Origin):
            line.amount += line.origin.pending_amount
            line.save()

    @classmethod
    def suggested_to_proposed(cls, lines):
        OriginSuggestedLine = Pool().get('account.statement.origin.suggested.line')
        suggested_lines = [line.suggested_line for line in lines
            if line.suggested_line and line.suggested_line.state != 'proposed']
        OriginSuggestedLine.propose(suggested_lines)


class Origin(Workflow, metaclass=PoolMeta):
    __name__ = 'account.statement.origin'

    journal = fields.Function(fields.Many2One('account.statement.journal', 'Journal'),
            'get_journal', searcher='search_journal')
    entry_reference = fields.Char("Entry Reference", readonly=True)
    suggested_lines = fields.One2Many(
        'account.statement.origin.suggested.line', 'origin',
        'Suggested Lines', states={
            'readonly': Bool(Eval('synchronized', False)),
            })
    suggested_lines_tree = fields.Function(
        fields.Many2Many('account.statement.origin.suggested.line', None, None,
            'Suggested Lines', states={
                'readonly': Bool(Eval('synchronized', False)),
                }), 'get_suggested_lines_tree')
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
    synchronized = fields.Function(fields.Boolean('Synchronized'),
        'on_change_with_synchronized')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.number.search_unaccented = False
        cls._order.insert(0, ('date', 'ASC'))
        cls._order.insert(1, ('number', 'ASC'))

        _sync_readonly = Bool(Eval('synchronized', False))
        _state_readonly = ~Eval('statement_state', '').in_(['draft',
                    'registered'])
        cls.statement.states['readonly'] = _sync_readonly
        cls.number.states['readonly'] = _sync_readonly
        cls.date.states['readonly'] = _sync_readonly
        cls.amount.states['readonly'] = _sync_readonly
        cls.amount_second_currency.states['readonly'] = _state_readonly
        cls.second_currency.states['readonly'] = _state_readonly
        cls.party.states['readonly'] = _state_readonly
        cls.account.states['readonly'] = _state_readonly
        cls.description.states['readonly'] = _state_readonly
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
                    'icon': 'tryton-add',
                    },
                'multiple_move_lines': {
                    'invisible': Eval('state') != 'registered',
                    'depends': ['state'],
                    'icon': 'tryton-add',
                    },
                'register': {
                    'invisible': Eval('state') != 'cancelled',
                    'depends': ['state'],
                    'icon': 'tryton-forward',
                    },
                'post': {
                    'invisible': Eval('state') != 'registered',
                    'depends': ['state'],
                    'icon': 'tryton-ok',
                    },
                'cancel': {
                    'invisible': Eval('state') == 'cancelled',
                    'depends': ['state'],
                    'icon': 'tryton-cancel',
                    },
                'search_suggestions': {
                    'invisible': Eval('state') != 'registered',
                    'depends': ['state'],
                    'icon': 'tryton-search',
                    },
                'link_invoice': {
                    'invisible': Eval('state') != 'registered',
                    'depends': ['state'],
                    'icon': 'tryton-link',
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

    @fields.depends('statement', '_parent_statement.journal')
    def on_change_with_synchronized(self, name=None):
        if self.statement and self.statement.journal.enable_banking_session:
            return True
        return False

    def get_journal(self, name):
        return self.statement.journal

    @classmethod
    def search_journal(cls, name, clause):
        return [('statement.' + clause[0],) + tuple(clause[1:])]


    def get_suggested_lines_tree(self, name):
        # return only parent lines in origin suggested lines
        # Not children.
        suggested_lines = []

        def _get_children(line):
            if line.parent is None:
                suggested_lines.append(line)

        for line in self.suggested_lines:
            _get_children(line)

        #return [x.id for x in suggested_lines if x.state == 'proposed']
        return suggested_lines

    def get_remittance_information(self, name):
        return (self.information.get('remittance_information', '')
            if self.information else '')

    def get_information_value(self, field):
        if self.information:
            return self.information.get(field, '')
        return ''

    @classmethod
    def search_remittance_information(cls, name, clause):
        pool = Pool()
        StatementOrigin = pool.get('account.statement.origin')

        database = Transaction().database

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
            where=(database.unaccent(remittance_information_column).ilike(
                    database.unaccent(value))))
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
        InvoiceTax = pool.get('account.invoice.tax')
        Warning = pool.get('res.user.warning')

        paid_cancelled_invoice_lines = []
        for origin in origins:
            origin.validate_amount()

            for line in origin.lines:
                if line.related_to:
                    # Try to find if the related_to is used in another
                    # posted origin, may be from the account move or from the
                    # possible realted invoice. But with the tax exception.
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
                                line.move_line.move_origin, Invoice)
                            and (not line.move_line.origin
                                or not isinstance(
                                line.move_line.origin, InvoiceTax))):
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
        StatementSuggestion = pool.get(
            'account.statement.origin.suggested.line')
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
            amount = ZERO
            amount_second_currency = ZERO
            statement = lines[0].statement if lines else None
            for line in lines:
                # If statement line related_to is a payment_group or a payment
                # and the clearing account is not deffined, need to create the
                # move line from the payment relateds.
                if (line.payment_group
                        and not line.payment_group.journal.clearing_account):
                    movelines = line.get_payment_group_move_line()
                else:
                    movelines = [(line.get_move_line(), None)]

                for move_line, payment in movelines:
                    move_line.move = move
                    amount += move_line.debit - move_line.credit
                    if line.amount_second_currency:
                        amount_second_currency += (
                            move_line.amount_second_currency)
                    move_lines.append((move_line, line, payment))

            if statement:
                move_line = statement._get_move_line(
                    amount, amount_second_currency, lines)
                move_line.move = move
                move_lines.append((move_line, None, None))

        if move_lines:
            MoveLine.save([x for x, _, _ in move_lines])

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
                if line.invoice and line.invoice_amount_to_pay != 0:
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
                        realted_to=str(lines_not_allowed[0].related_to),
                        origin=(lines_not_allowed[0].origin.rec_name
                            if lines_not_allowed[0].origin else '')))
            if lines_to_remove:
                StatementLine.delete(lines_to_remove)

            suggestions_to_remove = StatementSuggestion.search([
                    ('related_to', 'in', related_tos),
                    ('id', 'not in', suggested_ids),
                    ])
            if suggestions_to_remove:
                StatementSuggestion.delete(suggestions_to_remove)
        # Before reconcile ensure the moves are posted to avoid that some
        # possible estra moves, like writeoff, exchange, won't be posted.
        Move.post([m for m, _ in moves])
        # Reconcile at the end to avoid problems with the related_to lines
        if move_lines:
            StatementLine.reconcile(move_lines)
        return moves

    def similar_parties_query(self, text):
        pool = Pool()
        Party = pool.get('party.party')
        Rule = pool.get('ir.rule')

        party_table = Party.__table__()
        cursor = Transaction().connection.cursor()

        if not text:
            return {}

        database = Transaction().database

        similarity = Similarity(database.unaccent(party_table.name),
            database.unaccent(text))
        if hasattr(Party, 'trade_name'):
            similarity = Greatest(similarity,
                Similarity(database.unaccent(party_table.trade_name),
                    database.unaccent(text)))

        where = ((similarity >= PARTY_SIMILARITY_THRESHOLD)
            & (party_table.active))

        # If party_comapny module is installed, ensure that when try to search
        # suggestions called by cron, user id 0, not search on parties not
        # allowed ny the comapny.
        if hasattr(Party, 'companies'):
            PartyCompany = pool.get('party.company.rel')
            party_comapny_table = PartyCompany.__table__()
            company_query = party_comapny_table.select(
                party_comapny_table.party,
                where=party_comapny_table.company == self.company.id)
            where &= (party_table.id.in_(company_query))

        query = party_table.select(party_table.id, similarity, where=where)
        cursor.execute(*query)

        records = cursor.fetchall()

        # Use search in order for ir.rule to be applied
        with Transaction().set_context(_check_access=True):
            domain = Rule.domain_get(Party.__name__, mode='read')
        parties = Party.search(domain + [
                ('id', 'in', [x[0] for x in records]),
                ])
        similars = []
        for party in parties:
            percent = compare_party(party.name, text)
            similars.append((party, percent))
        return dict((x[0], x[1]) for x in sorted(similars, key=lambda x: x[1],
            reverse=True))

    def similar_parties(self):
        """
        This function returns a dictionary with the possible parties ID on
        'key' and the similarity on 'value'.
        It compares the 'information' (remittance information) value with the
        parties' name, based on the similarity values defined on the journal.
        Additionaly, compare the creditor's or debtor's name, depending if the
        amount is positive or negative, respectively.
        If a party appears during both searches, the greatest similarity is taken
        """
        debtor_creditor = None
        if self.amount > 0:
            debtor_creditor = self.get_information_value('debtor_name')
        elif self.amount < 0:
            debtor_creditor = self.get_information_value('creditor_name')
        information = self.remittance_information

        parties = {}

        parties = self.similar_parties_query(information)
        for party, value in self.similar_parties_query(debtor_creditor).items():
            parties[party] = max(parties.get(party, 0), value)

        if parties:
            # Discard all entries that have a similiarity below ~25% of the
            # maximum similarity. For cases where the maximum similarity is
            # small, allow a larger margin.
            maximum = next(iter(parties.values()))
            minimal = maximum - max(maximum / 4, 10)
            # Discard all entries that have a similarity below minimal
            parties = {k: v for k, v in parties.items() if v >= minimal}

            # We discard the party of the company at the very end (instead of
            # the SQL Query) on purpose if the text is very similar to the company
            # name it means that very probably it just refers to the company
            # so we do not want to spend our CPU cycles analyzing other parties
            # If we discarded the company name in the query, then the second best
            # party would be picked which may be very different from the company name
            # The previous logic is to avoid that
            parties.pop(self.company.party, None)

        return parties

    def similar_origins(self):
        pool = Pool()
        Statement = pool.get('account.statement')
        Origin = pool.get('account.statement.origin')
        Line = pool.get('account.statement.line')
        Rule = pool.get('ir.rule')

        statement_table = Statement.__table__()
        origin_table = Origin.__table__()
        line_table = Line.__table__()
        cursor = Transaction().connection.cursor()
        database = Transaction().database

        ORIGIN_SIMILARITY_THRESHOLD = self.statement.journal.get_weight(
            'origin-similarity-threshold')
        ORIGIN_DELTA_DAYS = self.statement.journal.get_weight(
            'origin-delta-days')

        similarity_column = Similarity(database.unaccent(JsonbExtractPathText(
                origin_table.information, 'remittance_information')),
            database.unaccent(self.remittance_information))
        query = origin_table.join(line_table,
            condition=origin_table.id == line_table.origin).join(
                statement_table,
                condition=origin_table.statement == statement_table.id).select(
            origin_table.id, similarity_column,
            where=((similarity_column >= ORIGIN_SIMILARITY_THRESHOLD / 100)
                & (statement_table.company == self.company.id)
                & (origin_table.state == 'posted')
                & (line_table.related_to == None))
                & (origin_table.create_date >= self.date
                   - timedelta(days=ORIGIN_DELTA_DAYS)),
            order_by=[similarity_column.desc]
            )
        cursor.execute(*query)
        records = cursor.fetchall()
        # Use search in order for ir.rule to be applied
        with Transaction().set_context(_check_access=True):
            domain = Rule.domain_get(Origin.__name__, mode='read')
        origins = Origin.search(domain + [
                ('id', 'in', [x[0] for x in records]),
                ])
        similarities = [x[1] * 100 for x in records]
        merge = list(zip(origins, similarities))
        if merge:
            # Discard all entries that have a similiarity below ~25% of the
            # maximum similarity. For cases where the maximum similarity is
            # small, allow a larger margin.
            maximum = merge[0][1]
            minimal = maximum - max(maximum / 4, 10)
            # Discard all entries that have a similarity below minimal
            merge = [x for x in merge if x[1] >= minimal]
        return merge

    def get_suggestions_from_payments(self, payments, group_key=(), type_=''):
        """
        Create one or more suggested registers based on the move_lines.
        If there are more than one move_line, it will be grouped under
        a parent.
        """
        pool = Pool()
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        if not payments:
            return []

        to_save = []
        group = group_key[0] if group_key else None
        date = group_key[1] if group_key else None
        if group:
            line = SuggestedLine()
            line.type = type_
            line.origin = self
            line.date = date
            line.related_to = group
            line.amount = sum(payment.amount if payment.kind == 'receivable'
                else -payment.amount for payment in payments)
            to_save.append(line)
        else:
            for payment in payments:
                line = SuggestedLine()
                line.type = type_
                line.origin = self
                if payment.line:
                    line.account = payment.line.account
                else:
                    if payment.kind == 'payable':
                        line.account = payment.party.payable_account
                    else:
                        line.account = payment.party.receivable_account
                line.party = payment.party
                line.date = payment.date
                line.related_to = payment
                line.amount = (payment.amount if payment.kind == 'receivable'
                    else -payment.amount)
                to_save.append(line)

        suggested_line = SuggestedLine.pack(to_save)
        return [suggested_line] if suggested_line else []

    def get_suggestion_from_move_line(self, line):
        pool = Pool()
        SuggestedLine = pool.get('account.statement.origin.suggested.line')
        Invoice = pool.get('account.invoice')

        move_origin = line.move_origin
        if (isinstance(move_origin, Invoice) and move_origin.state != 'paid'
                and move_origin.account == line.account):
            related_to = line.move_origin
        else:
            related_to = line
        if (related_to and related_to.__name__ not in
                SuggestedLine.related_to.domain.keys()):
            related_to = None
        return SuggestedLine(
            origin=self,
            party=line.party,
            date=line.maturity_date or line.date,
            related_to=related_to,
            account=line.account,
            amount=line.debit - line.credit,
            second_currency=line.second_currency,
            amount_second_currency=line.amount_second_currency,
            )

    def get_suggestion_from_move_lines(self, move_lines, type_, based_on=None):
        pool = Pool()
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        if not move_lines:
            return []

        to_save = []
        for line in move_lines:
            suggested_line = self.get_suggestion_from_move_line(line)
            suggested_line.based_on = based_on
            suggested_line.type = type_
            to_save.append(suggested_line)

        return SuggestedLine.pack(to_save)

    def _suggest_clearing_payment_group(self):
        pool = Pool()
        Group = pool.get('account.payment.group')
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        if not self.pending_amount:
            return

        to_save = []
        kind = 'receivable' if self.pending_amount > ZERO else 'payable'
        for group in Group.search([
                ('journal.currency', '=', self.currency),
                ('journal.clearing_account', '!=', None),
                ('company', '=', self.company.id),
                ('kind', '=', kind),
                ]):
            if group.payment_amount != abs(self.pending_amount):
                continue
            for payment in group.payments:
                if (payment.state == 'failed' or (payment.state != 'failed'
                            and payment.line and payment.line.reconciliation)):
                    break
            else:
                suggestion = SuggestedLine()
                suggestion.origin = self
                suggestion.type = 'payment-group'
                suggestion.related_to = group
                suggestion.date = group.planned_date
                suggestion.amount = group.total_amount
                suggestion.account = group.journal.clearing_account
                # TODO: Is this second_currency necessary?
                suggestion.second_currency = self.second_currency
                to_save.append(suggestion)

        SuggestedLine.save(to_save)

    def _suggest_clearing_payment(self):
        pool = Pool()
        Payment = pool.get('account.payment')
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        if not self.pending_amount:
            return

        to_save = []
        for payment in Payment.search([
                ('currency', '=', self.currency),
                ('company', '=', self.company.id),
                ('state', '!=', 'failed'),
                ('journal.clearing_account', '!=', None),
                ('clearing_move', '!=', None),
                ('amount', '=', self.pending_amount),
                ]):
            suggested_lines = self.get_suggestions_from_payments([payment],
                group_key=(), type_='payment')
            if suggested_lines:
                to_save += suggested_lines

        SuggestedLine.save(to_save)

    def _suggest_payment(self):
        pool = Pool()
        Payment = pool.get('account.payment')
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        amount = self.pending_amount
        if not amount:
            return

        to_save = []
        groups = {
            'amount': ZERO,
            'groups': {}
            }
        domain = [
            ('company', '=', self.company.id),
            ('state', '!=', 'failed'),
            ('line', '!=', None),
            ('line.reconciliation', '=', None),
            ('line.account.reconcile', '=', True),
            ]
        if self.second_currency:
            domain.append(('currency', '=', self.second_currency))
        else:
            domain.append(('currency', '=', self.currency))
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

            # Some Banks group payments by different, but consecutive dates.
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

        if groups['amount'] == abs(amount) and len(groups['groups']) > 1:
            payments = [
                p for v in groups['groups'].values() for p in v['payments']]
            suggested_lines = self.get_suggestions_from_payments(payments,
                group_key=(), type_='payment-group')
            if suggested_lines:
                to_save += suggested_lines
        elif groups['amount'] != ZERO:
            for key, item in groups['groups'].items():
                if item['amount'] == abs(amount):
                    suggested_lines = self.get_suggestions_from_payments(item['payments'],
                        group_key=key, type_='payment-group')
                    if suggested_lines:
                        to_save += suggested_lines

        SuggestedLine.save(to_save)

    def _search_move_line_reconciliation_domain(self, second_currency=None):
        domain = [
            ('move.company', '=', self.company.id),
            ('currency', '=', self.currency),
            ('move_state', '=', 'posted'),
            ('reconciliation', '=', None),
            ('account.reconcile', '=', True),
            ('invoice_payment', '=', None),
            ['OR',
                ('debit', '!=', 0),
                ('credit', '!=', 0),
            ]]
        if second_currency:
            domain.append(('second_currency', '=', second_currency))
        return domain

    def _suggest_origin_key(self, line):
        return (line.account.id if line.account else -1,
            line.party.id if line.party else -1)

    def get_suggestion_from_origin_key(self, origin, key):
        pool = Pool()
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        suggestion = SuggestedLine()
        suggestion.origin = self
        suggestion.type = 'origin'
        suggestion.account = key[0]
        suggestion.party = key[1] if key[1] >= 0 else None
        suggestion.amount = ZERO
        suggestion.amount_second_currency = ZERO
        suggestion.based_on = origin
        return suggestion

    def _suggest_origin(self):
        """
        Search for old origin lines. Reproducing the same line/s created.
        """
        pool = Pool()
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        suggested_lines = []

        amount = self.pending_amount
        if not amount or not self.remittance_information or not self.company:
            return suggested_lines

        last_similarity = 0
        for origin, similarity in self.similar_origins():
            if similarity == last_similarity:
                continue
            last_similarity = similarity

            def get_key(x):
                return (x.account.id if x.account else -1,
                    x.party.id if x.party else -1)

            suggestions = []
            for key, group in groupby(sorted(origin.lines,
                            key=self._suggest_origin_key),
                        key=self._suggest_origin_key):
                suggestion = self.get_suggestion_from_origin_key(origin, key)
                suggestions.append(suggestion)

            if len(suggestions) == 1:
                suggestions[0].amount = amount

            SuggestedLine.pack(suggestions)

    def _suggest_combination(self, domain, type_, based_on=None,
            sorting='oldest'):
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        Rule = pool.get('ir.rule')

        MAX_LENGTH = self.statement.journal.get_weight('move-line-max-count')

        # TODO: Make DECIMALS depend on the currency
        DECIMALS = 2
        POWER = 10 ** DECIMALS

        # Converting all Decimal to int to improve performance
        # In several tests reduced the time by 30%
        def to_int(value):
            return int(value * POWER)

        max_tolerance = to_int(self.statement.journal.max_amount_tolerance)

        # TODO: Add support for second_currency
        amount = to_int(self.pending_amount)
        if not amount:
            return

        if self.escape():
            return

        lines = MoveLine.search(domain)
        if sorting == 'oldest':
            lines = sorted(lines, key=lambda x: x.maturity_date or x.date)
        else: # sorting == 'closest'
            lines = sorted(lines, key=lambda x: abs(self.date -
                    (x.maturity_date or x.date)))
        # Use search in order for ir.rule to be applied
        with Transaction().set_context(_check_access=True):
            domain = Rule.domain_get(MoveLine.__name__, mode='read')
        lines = MoveLine.search(domain + [
                ('id', 'in', [x.id for x in lines]),
                ])
        if not lines:
            return

        target_combinations = self.journal.get_weight('target-combinations')

        found = 0
        lines = tuple((x, to_int(x.debit - x.credit)) for x in lines)
        for length in range(1, min(MAX_LENGTH, len(lines)) + 1):
            if length > len(lines):
                break
            if length == 1:
                candidates = lines
            else:
                l = candidate_size(length, target_combinations)
                candidates = lines[:l]
            for combination in combinations(candidates, length):
                total_amount = sum(x[1] for x in combination)
                if abs(total_amount - amount) <= max_tolerance:
                    self.get_suggestion_from_move_lines(
                        [x[0] for x in combination], type_, based_on=based_on)

                    found += 1
                    if type_ == 'combination-party':
                        break
                    if self.escape():
                        return
            # If we already found some lines we want to quit. The
            # larger the number of combinations, the sooner we want to
            # quit because the probability of finding a good
            # combination is lower and the computational cost not worth
            # it
            #if len(to_save) >= (MAX_LENGTH - length) / 5:
                #return

    def _suggest_similar_parties(self):
        Party = Pool().get('party.party')

        similar_parties = set((x,) for x in list(self.similar_parties().keys())[:5])
        for similar_origin, _ in self.similar_origins()[:5]:
            similar_parties.add(tuple(sorted(set(x.party for x in
                            similar_origin.lines if x.party))))

        for parties in similar_parties:
            parties = Party.browse(parties)
            domain = self._search_move_line_reconciliation_domain()
            domain.append(('party', 'in', parties))
            # Execute closest first because it can rank better
            self._suggest_combination(domain, 'combination-party',
                sorting='closest')
            self._suggest_combination(domain, 'combination-party',
                sorting='oldest')

    def _suggest_combination_all(self):
        # This suggestion could be very time-consuming, so only execute it if
        # the weight is non-zero
        if not self.journal.get_weight('type-combination-all'):
            return
        domain = self._search_move_line_reconciliation_domain()
        # Execute closest first because it can rank better
        self._suggest_combination(domain, 'combination-all', sorting='closest')
        self._suggest_combination(domain, 'combination-all', sorting='oldest')

    def _suggest_balance(self):
        pool = Pool()
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        if not self.similar_parties():
            return

        party = list(self.similar_parties().keys())[0]
        party_similarity = self.similar_parties()[party]
        if party_similarity <= 90:
            return

        suggested_line = SuggestedLine()
        suggested_line.origin = self
        suggested_line.type = 'balance'
        suggested_line.party = party
        suggested_line.amount = self.pending_amount
        if self.pending_amount > 0:
            suggested_line.account = party.account_receivable_used
        else:
            suggested_line.account = party.account_payable_used
        suggested_line.date = self.date
        suggested_line.save()

    def _suggest_balance_old_invoices(self):
        pool = Pool()
        Invoice = pool.get('account.invoice')
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        if not self.similar_parties():
            return

        party = list(self.similar_parties().keys())[0]
        party_similarity = self.similar_parties()[party]
        if party_similarity <= 90:
            return

        amount = self.pending_amount
        suggestions = []
        for invoice in Invoice.search([
                ('company', '=', self.company.id),
                ('party', '=', party.id),
                ('state', '=', 'posted'),
                ], order=[('invoice_date', 'ASC')]):
            amount_to_pay = invoice.amount_to_pay
            if invoice.type == 'in':
                amount_to_pay *= -1
            if amount > 0 and amount_to_pay <= 0:
                continue
            if amount < 0 and amount_to_pay >= 0:
                continue

            if amount > 0:
                assigned = min(amount, amount_to_pay)
            else:
                assigned = max(amount, amount_to_pay)
            suggestion = SuggestedLine()
            suggestion.origin = self
            suggestion.type = 'balance-invoice'
            suggestion.date = self.date
            suggestion.party = party
            suggestion.related_to = invoice
            suggestion.account = invoice.account
            suggestion.amount = assigned
            suggestions.append(suggestion)
            amount -= assigned
            if not amount:
                break

        if amount:
            suggestion = SuggestedLine()
            suggestion.origin = self
            suggestion.type = 'balance-invoice'
            suggestion.date = self.date
            suggestion.party = party.id
            suggestion.amount = amount
            if self.pending_amount > 0:
                suggestion.account = party.account_receivable_used
            else:
                suggestion.account = party.account_payable_used
            suggestion.date = self.date
            suggestions.append(suggestion)

        SuggestedLine.pack(suggestions)

    def merge_suggestions(self):
        SuggestedLine = Pool().get('account.statement.origin.suggested.line')
        SuggestedLine.merge_suggestions(self.suggested_lines_tree)

    def escape(self):
        SuggestedLine = Pool().get('account.statement.origin.suggested.line')

        lines = SuggestedLine.search([
                ('origin', '=', self),
                ('parent', '=', None),
                ('state', '=', 'proposed'),
                ], order=[('weight', 'DESC')])
        if not lines:
            return False
        ESCAPE = self.statement.journal.get_weight('escape-threshold')
        if lines[0].weight > ESCAPE:
            return True
        COMBINATION_ESCAPE = self.statement.journal.get_weight(
            'combination-escape-threshold')
        for line in lines:
            if (line.type.startswith('combination')
                    and line.weight >= COMBINATION_ESCAPE):
                return True
        return False

    @classmethod
    @ModelView.button
    def search_suggestions(cls, origins):
        pool = Pool()
        SuggestedLine = pool.get('account.statement.origin.suggested.line')
        StatementLine = pool.get('account.statement.line')

        if not origins:
            return

        #Lock the statement origins before search suggestions, because the
        #search process is consuming so much time, and the user could try
        #to post the origin while the suggestion search is not finished.
        #Without the lock this could be done and after the suggestion save
        #the lines detected and break the pending_amount == 0.
        cls.lock(origins)

        # Before a new search remove all suggested lines, but control if any
        # of them are related to a statement line.
        suggestions = SuggestedLine.search([
                ('origin', 'in', origins),
                ])
        if suggestions:
            lines = StatementLine.search([
                    ('suggested_line', 'in', suggestions)
                    ])
            if lines:
                origins_name = ", ".join([x.origin.rec_name
                        for x in lines if x.origin])
                raise AccessError(
                    gettext('account_statement_enable_banking.'
                        'msg_suggested_line_related_to_statement_line',
                        origins_name=origins_name))

        SuggestedLine.delete(suggestions)

        count = 0
        to_use = []
        for origin in origins:
            count += 1
            if origin.pending_amount == ZERO:
                continue

            origin._suggest_clearing_payment_group()
            origin._suggest_clearing_payment()
            origin._suggest_payment()
            origin._suggest_balance()
            origin._suggest_balance_old_invoices()
            origin._suggest_origin()
            origin._suggest_similar_parties()
            origin._suggest_combination_all()

            ORIGIN_SIMILARITY = origin.statement.journal.get_weight(
                'origin-similarity')

            origin.merge_suggestions()
            best = SuggestedLine.search([
                    ('origin', '=', origin),
                    ('parent', '=', None),
                    ('weight', '>=', ORIGIN_SIMILARITY)
                    ], order=[('weight', 'DESC')], limit=2)
            if not best:
                continue
            first = best[0]
            second = best[-1]
            # Only use the first suggestion if it has a greater weight than the
            # second one
            if first == second or first.weight > second.weight:
                to_use.append(first)

        if to_use:
            SuggestedLine.use(to_use)

        return
        # Trim remaining suggestions to a max of the best 10
        origins_to_save = []
        for origin in origins:
            if len(origin.suggested_lines_tree) <= 10:
                continue
            parent_suggestions = SuggestedLine.search([
                    ('origin', '=', origin),
                    ('parent', '=', None),
                    ], order=[('weight', 'DESC')], limit=10)
            child_suggestions = SuggestedLine.search([
                    ('origin', '=', origin),
                    ('parent', 'in', [x.id for x in parent_suggestions]),
                    ])
            origin.suggested_lines = parent_suggestions + child_suggestions
            origins_to_save.append(origin)

        if origins_to_save:
            cls.save(origins_to_save)

    @classmethod
    def _get_statement_line(cls, origin, related):
        pool = Pool()
        StatementLine = pool.get('account.statement.line')
        Invoice = pool.get('account.invoice')
        Date = pool.get('ir.date')
        Currency = pool.get('currency.currency')

        maturity_date = None
        if isinstance(related, Invoice):
            with Transaction().set_context(with_payment=False):
                invoice, = Invoice.browse([related])
            sign = -1 if invoice.type == 'in' else 1
            amount = sign * invoice.amount_to_pay
            second_currency = invoice.currency
            if origin.second_currency:
                second_currency_date = invoice.currency_date or Date.today()
                with Transaction().set_context(date=second_currency_date):
                    amount_to_pay = Currency.compute(second_currency,
                        invoice.amount_to_pay, origin.company.currency,
                        round=True)
                amount_second_currency = sign * amount_to_pay
            else:
                amount_second_currency = sign * invoice.amount_to_pay
            lines_to_pay = [l for l in related.lines_to_pay
                if l.maturity_date and l.reconciliation is None]
            oldest_line = (min(lines_to_pay,
                    key=lambda line: line.maturity_date)
                if lines_to_pay else None)
            if oldest_line:
                maturity_date = oldest_line.maturity_date
        else:
            amount=related.amount
            second_currency = related.second_currency
            amount_second_currency = related.amount_second_currency
            maturity_date = related.maturity_date

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
        line.maturity_date = maturity_date
        line.description = origin.remittance_information
        return line

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
    @ModelView.button_action(
            'account_statement_enable_banking.wizard_link_invoice')
    def link_invoice(cls, origins):
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

        cls.find_same_related_origin(origins)
        cls.validate_origin(origins)
        cls.create_moves(origins)

        lines = [x for o in origins for x in o.lines]
        # It's an awful hack to set the state, but it's needed to ensure the
        # Error of statement state in Move.post is not applied when trying to
        # concile an individual origin. For this, need the state == 'posted'.
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
        StatementLine = pool.get('account.statement.line')

        lines = [x for origin in origins for x in origin.lines]
        StatementLine.cancel_lines(lines)

    @classmethod
    def find_same_related_origin(cls, origins):
        StatementLine = Pool().get('account.statement.line')
        SuggestedLine = Pool().get('account.statement.origin.suggested.line')

        relateds_to = {}
        for origin in origins:
            for line in origin.lines:
                if not line.related_to:
                    continue
                if line.related_to not in relateds_to:
                    if line.invoice:
                        amount = line.invoice_amount_to_pay
                    elif line.payment:
                        amount = line.payment.amount
                    elif line.move_line:
                        amount = line.move_line.debit - line.move_line.credit
                    elif line.payment_group:
                        amount = line.payment_group.payment_amount
                    else:
                        amount = Decimal(0)
                    diff = amount - line.amount
                    relateds_to[line.related_to] = {
                        'amount': amount,
                        'diff': diff,
                        }
                else:
                    relateds_to[line.related_to]['diff'] -= line.amount
        for related_to, values in relateds_to.items():
            if ((values['amount'] >= 0 and values['diff'] < 0)
                    or (values['amount'] < 0 and values['diff'] > 0)):
                raise AccessError(gettext('account_statement_enable_banking.'
                        'msg_find_same_related_to',
                        related_to=(related_to.rec_name
                            if not isinstance(related_to, 'str')
                            else related_to)))

        lines = StatementLine.search([
            ('related_to', 'in', relateds_to),
            ('origin', 'not in', origins),
            ('statement.state', '=', 'draft'),
            ])
        suggested_lines_to_delete = []
        lines_to_delete = []
        for line in lines:
            values = relateds_to[line.related_to]
            if ((values['amount'] >= 0 and values['diff'] - line.amount < 0)
                    or (values['amount'] < 0
                        and values['diff'] - line.amount > 0)):
                lines_to_delete.append(line)
                suggested_lines_to_delete.extend([line.suggested_line
                        for line in lines if line.suggested_line])
        StatementLine.delete(lines)
        SuggestedLine.delete(suggested_lines_to_delete)


class OriginSuggestedLine(Workflow, ModelSQL, ModelView, tree()):
    'Account Statement Origin Suggested Line'
    __name__ = 'account.statement.origin.suggested.line'

    type = fields.Selection([
            (None, ''),
            ('combination-party', 'Combine Parties'),
            ('combination-all', 'Combine All'),
            ('payment-group', 'Payment Group'),
            ('payment', 'Payment'),
            ('origin', 'Origin'),
            ('balance', 'Balance'),
            ('balance-invoice', 'Balance Invoices'),
            ], 'Type', readonly=True, states={
            'required': ~Bool(Eval('parent')),
            'invisible': Bool(Eval('parent')),
            })
    type_string = type.translated('type')
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
                ],
            })
    weight = fields.Integer('Weight', required=True)
    state = fields.Selection([
            ('proposed', "Proposed"),
            ('used', "Used"),
            ], "State", readonly=True, sort=False)
    based_on = fields.Many2One('account.statement.origin', 'Based On')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order.insert(0, ('weight', 'DESC'))
        cls._order.insert(1, ('date', 'ASC'))
        cls._order.insert(2, ('account', 'ASC'))
        cls._order.insert(3, ('party', 'ASC'))
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

    @classmethod
    def __register__(cls, module_name):
        table_h = cls.__table_handler__(module_name)
        if (table_h.column_exist('similarity')
                and not table_h.column_exist('weight')):
            table_h.column_rename('similarity', 'weight')
        super().__register__(module_name)

    @staticmethod
    def default_state():
        return 'proposed'

    @staticmethod
    def default_weight():
        return 0

    @staticmethod
    def default_amount():
        return ZERO

    def get_rec_name(self, name):
        if self.related_to:
            return self.related_to.rec_name
        return self.type_string

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
    def create(cls, vlist):
        suggestions = super().create(vlist)
        for suggestion in suggestions:
            suggestion.update_weight()
        cls.save(suggestions)
        return suggestions

    @classmethod
    def write(cls, *args):
        super().write(*args)
        actions = iter(args)
        to_save = []
        for suggestions, values in zip(actions, actions):
            if 'weight' not in values.keys():
                for suggestion in suggestions:
                    suggestion.update_weight()
                    to_save.append(suggestion)
        cls.save(to_save)

    def update_weight(self):
        pool = Pool()
        Invoice = pool.get('account.invoice')
        MoveLine = pool.get('account.move.line')
        Payment = pool.get('account.payment')

        self.weight = 0

        journal = self.origin.statement.journal
        if not self.parent:
            TYPE_WEIGHTS = {
                None: 0,
                'combination-party': journal.get_weight(
                    'type-combination-party'),
                'combination-all': journal.get_weight('type-combination-all'),
                'payment-group': journal.get_weight('type-payment-group'),
                'payment': journal.get_weight('type-payment'),
                'origin': journal.get_weight('type-origin'),
                'balance': journal.get_weight('type-balance'),
                'balance-invoice': journal.get_weight('type-balance-invoice'),
                }
            self.weight += TYPE_WEIGHTS[self.type]

        if self.childs:
            # TODO: If a suggestion has children, the larger the combination, the
            # lower the weight of the suggestion
            # Also, if there are several children with different parties, the lower
            # the weight of the suggestion

            # If the suggestion has childs, the weight is the sum of the childs
            self.weight += sum(child.weight for child in self.childs) / len(self.childs)
            return

        origin = self.origin

        # Update weight based on dates
        if origin and self.date:
            DATE_WEIGHT = journal.get_weight('date-match')
            dates = set([origin.date])
            value_date = origin.information and origin.information.get('value_date')
            if value_date:
                dates.add(datetime.strptime(value_date, '%Y-%m-%d').date())

            # If the difference in days is zero, the weight will be DATE_WEIGHT
            # then, it will be increasingly lower based on a normal
            # distribution with standard deviation of 10 days. This means that
            # if the difference is 10 days, the weight will be DATE_WEIGHT / 2,
            # and if the difference is 20 days, the weight will be DATE_WEIGHT
            # / 4, and so on.
            dw = set()
            for date in dates:
                days = abs(self.date - date).days
                dw.add(gaussian_score(days, 0, 10))
            self.weight += int(round(DATE_WEIGHT * max(dw)))

        # Update weight based on party
        if origin and self.party:
            PARTY_WEIGHT = journal.get_weight('party-match')
            # Scale the similarity down to a value between 0 and 20
            similar_parties = origin.similar_parties()
            self.weight += int(round(PARTY_WEIGHT * (similar_parties.get(
                self.party.id, 0) / 100)))

        # Update weight based on invoice number
        invoice = None
        if isinstance(self.related_to, Invoice):
            invoice = self.related_to
        elif (isinstance(self.related_to, MoveLine)
                and isinstance(self.related_to.move_origin, Invoice)):
            invoice = self.related_to.move_origin
        elif (isinstance(self.related_to, Payment) and self.related_to.line
                and isinstance(self.related_to.line.move_origin, Invoice)):
            invoice = self.related_to.line.move_origin

        if invoice:
            if invoice.type == 'out':
                number = invoice.number
            else:
                number = invoice.reference
            if number:
                NUMBER_WEIGHT = journal.get_weight('number-match')
                # Scale the similarity down to a value between 0 and 20
                # Given that it relatively easy that two or three characters
                # match we use a normal distribution with a mean of the length
                # of the number and a standard deviation of half the length of
                # the number instead of computing a simple percentage so short
                # matching strings do not affect to much on the weight
                length = longest_common_substring(origin.remittance_information,
                    number)
                self.weight += int(round(NUMBER_WEIGHT * gaussian_score(length,
                            mean=len(number), stddev=len(number) / 4)))

        if self.based_on:
            BASED_ON_WEIGHT = journal.get_weight('based-on-match')
            length = longest_common_substring(origin.remittance_information,
                self.based_on.remittance_information)
            rl = len(self.origin.remittance_information)
            self.weight += int(round(BASED_ON_WEIGHT * gaussian_score(length,
                    mean=rl, stddev=rl / 4)))

    @classmethod
    def pack(cls, suggestions):
        'Creates a parent suggestion if more than one suggestion is provided'
        if not suggestions:
            return
        if len(suggestions) == 1:
            cls.save(suggestions)
            return suggestions[0]

        parent = cls()
        parent.origin = suggestions[0].origin
        parent.type = suggestions[0].type
        parent.date = None

        amount = ZERO
        for suggestion in suggestions:
            suggestion.type = None
            suggestion.parent = parent
            for field in ('date', 'related_to', 'party', 'account',
                    'second_currency', 'amount_second_currency', 'based_on'):
                if not hasattr(suggestion, field):
                    setattr(suggestion, field, None)
            suggestion.childs = []
            suggestion.update_weight()
            amount += suggestion.amount

        parent.childs = suggestions
        parent.amount = amount
        parent.save()
        return parent

    @classmethod
    def merge_suggestions(cls, suggestions):
        suggestions = [x for x in suggestions if not x.parent]
        def get_key(x):
            if not x.parent:
                children = tuple(sorted([get_key(y) for y in x.childs]))
            else:
                children = tuple()
            # We use str simply to prevent errors due to comparing None
            # with other things when sorting, as sorted is required for
            # groupby but also to ensure that two suggestions with the same
            # children have the same key
            return (str(x.origin), str(x.account), str(x.party),
                str(x.related_to), str(x.date), x.amount,
                str(x.second_currency), str(x.amount_second_currency),
                children)

        # Given that sort honours the original order of the elements
        # We first sort by weight, so the first element of each group
        # is the one with the highest weight, and the one to keep
        suggestions = sorted(suggestions, key=lambda x: x.weight, reverse=True)
        removing = 0
        to_delete = []
        for key, lines in groupby(sorted(suggestions, key=get_key),
                key=get_key):
            for line in list(lines)[1:]:
                to_delete += line.childs
                to_delete.append(line)
            to_delete += list(lines)[1:]
            removing += 1
        cls.delete(to_delete)

    def get_statement_line(self):
        pool = Pool()
        StatementLine = pool.get('account.statement.line')

        line = StatementLine()
        line.origin = self.origin
        line.statement = self.origin.statement
        line.suggested_line = self
        line.related_to = self.related_to
        line.party = self.party
        line.account = self.account
        line.amount = self.amount
        line.second_currency = self.second_currency
        line.amount_second_currency = self.amount_second_currency
        line.date = self.origin.date
        line.description = self.origin.remittance_information
        return line

    @classmethod
    @ModelView.button
    @Workflow.transition('used')
    def use(cls, suggestions):
        pool = Pool()
        StatementLine = pool.get('account.statement.line')
        MoveLine = pool.get('account.move.line')
        Warning = pool.get('res.user.warning')

        to_save = []
        to_warn = []
        for suggestion in suggestions:
            if suggestion.origin.state == 'posted':
                continue
            childs = suggestion.childs if suggestion.childs else [suggestion]
            for child in childs:
                if child.state == 'used':
                    continue
                # Parent state would be changed by Workflow.transition
                # but we need to change the childs state too
                child.state = 'used'

                related_to = child.related_to
                if (isinstance(related_to, MoveLine)
                        and related_to.payment_blocked):
                    to_warn.append(related_to)

                line = child.get_statement_line()
                to_save.append(line)

            cls.save(childs)

        if to_warn:
            names = ', '.join(x.rec_name for x in to_warn)
            if len(to_warn) > 5:
                names += '...'
            warning_key = Warning.format('use_move_line_payment_blocked',
                to_warn)
            if Warning.check(warning_key):
                raise UserWarning(warning_key,
                    gettext('account_statement_enable_banking.'
                        'msg_not_use_blocked_account_move',
                        move_lines=names))
        StatementLine.save(to_save)


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
            'amount': self.record.amount,
            'pending_amount': self.record.pending_amount,
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
    amount = Monetary("Amount", currency='currency', digits='currency',
        readonly=True)
    pending_amount = Monetary("Pending Amount", currency='currency', digits='currency',
        readonly=True)


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
            'amount': self.record.amount,
            'pending_amount': self.record.pending_amount,
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
    amount = Monetary("Amount", currency='currency', digits='currency',
        readonly=True)
    pending_amount = Monetary("Pending Amount", currency='currency', digits='currency',
        readonly=True)


class RetrieveEnableBankingSessionStart(ModelView):
    "Retrieve Enable Banking Session Start"
    __name__ = 'enable_banking.retrieve_session.start'

    enable_banking_session_valid_days = fields.TimeDelta(
        'Enable Banking Session Valid Days',
        states={
            'invisible': Eval('enable_banking_session_valid', False),
            }, help="Only allowed maximum 180 days.")
    enable_banking_session_valid = fields.Boolean(
        'Enable Banking Session Valid')

    @staticmethod
    def default_enable_banking_session_valid_days():
        return timedelta(days=180)


class RetrieveEnableBankingSessionSelect(ModelView):
    "Retrieve Enable Banking Session Select Session"
    __name__ = 'enable_banking.retrieve_session.select_session'

    found_session = fields.Many2One('enable_banking.session', "Found Session",
        readonly=True)
    enable_banking_session_valid_days = fields.TimeDelta(
        'Enable Banking Session Valid Days',
        states={
            'invisible': Eval('enable_banking_session_valid', False),
            }, help="Only allowed maximum 180 days.")
    enable_banking_session_valid = fields.Boolean(
        'Enable Banking Session Valid')

    @staticmethod
    def default_enable_banking_session_valid_days():
        return timedelta(days=180)


class LinkInvoiceStart(ModelView):
    'Link Invoice Start'
    __name__ = 'statement.link.invoice.start'

    company = fields.Many2One('company.company', "Company")
    currency = fields.Many2One('currency.currency', "Currency")
    invoice = fields.Many2One('account.invoice', 'Invoice', required=True,
        domain=[
            ('company', '=', Eval('company', -1)),
            ('currency', '=', Eval('currency', -1)),
            ('state', '=', 'posted'),
            ])
    invoice_amount = Monetary("Invoice Amount", currency='currency', digits='currency',
        readonly=True)
    origins_amount = Monetary("Origins Amount", currency='currency', digits='currency',
        readonly=True)
    diff_amount = Monetary("Difference Amount", currency='currency', digits='currency',
        readonly=True)
    post_origin = fields.Boolean("Post Origins")

    @fields.depends('invoice')
    def on_change_with_invoice_amount(self, name=None):
        if self.invoice:
            sign = -1 if self.invoice.type == 'in' else 1
            return sign * self.invoice.amount_to_pay

    @fields.depends('invoice', 'origins_amount')
    def on_change_with_diff_amount(self, name=None):
        if self.invoice:
            sign = -1 if self.invoice.type == 'in' else 1
            return (sign * self.invoice.amount_to_pay) - self.origins_amount


class LinkInvoice(Wizard):
    'Link Statement Origin Invoice'
    __name__ = 'statement.link.invoice'

    start = StateView('statement.link.invoice.start',
        'account_statement_enable_banking.\
        statement_link_invoice_start_view_form',
        [Button('Cancel', 'end', 'tryton-cancel'),
            Button('Apply', 'apply', 'tryton-ok')])
    apply = StateTransition()

    def default_start(self, fields):
        pool = Pool()
        StatementOrigin = pool.get('account.statement.origin')
        Warning = pool.get('res.user.warning')

        origins = StatementOrigin.browse(Transaction().context['active_ids'])
        origins_amount = sum(l.amount for l in origins)
        origin = origins[0]

        wnames = []
        for origin in origins:
            if getattr(origin, 'lines', []):
                wnames.append(origin)
            if len(wnames) == 5:
                break

        warning_name = 'origin_with_lines.' + hashlib.md5(
            ''.join([str(l.id) for l in wnames]).encode('utf-8')).hexdigest()
        names = ', '.join(l.number or str(l.id) for l in wnames)
        if len(wnames) == 5:
            names += '...'

        if Warning.check(warning_name):
            raise StatementValidateWarning(warning_name,
                gettext('account_statement_enable_banking.'
                    'msg_origins_with_lines',
                    origins=names))

        return {
            'company': origin.company.id,
            'currency': origin.currency.id,
            'origins_amount': origins_amount,
            'post_origin': True,
            }

    def transition_apply(self):
        pool = Pool()
        StatementOrigin = pool.get('account.statement.origin')
        SuggestedLine = pool.get('account.statement.origin.suggested.line')
        StatementLine = pool.get('account.statement.line')

        origins = StatementOrigin.browse(Transaction().context['active_ids'])
        invoice = self.start.invoice
        amount = invoice.amount_to_pay
        origins_amount = sum(l.amount for l in origins)
        if abs(amount) < abs(origins_amount):
            raise AccessError(gettext(
                    'account_statement_enable_banking.\
                    msg_not_enough_amount_to_pay',
                    amount_to_pay=amount,
                    origins_amount=origins_amount))

        lines = []
        suggestions_to_delete = []
        lines_to_delete = []
        for origin in origins:
            suggestions_to_delete += list(origin.suggested_lines or [])
            lines_to_delete += list(origin.lines or [])
            line = StatementOrigin._get_statement_line(origin, invoice)
            line.amount = origin.amount
            lines.append(line)
        StatementLine.delete(lines_to_delete)
        SuggestedLine.delete(suggestions_to_delete)
        StatementLine.save(lines)

        if self.start.post_origin:
            StatementOrigin.post(origins)

        return 'end'


class SynchronizeStatementEnableBankingStart(ModelView):
    "Synchronize Statement Enable Banking Start"
    __name__ = 'enable_banking.synchronize_statement.start'


class RetrieveEnableBankingSession(Wizard):
    "Retrieve Enable Banking Session"
    __name__ = 'enable_banking.retrieve_session'
    start_state = 'check_session_before_start'

    check_session_before_start = StateTransition()
    start = StateView('enable_banking.retrieve_session.start',
        'account_statement_enable_banking.'
        'enable_banking_retrieve_session_start_form',
        [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('OK', 'check_session', 'tryton-ok', default=True),
        ])
    check_session = StateTransition()
    select_session = StateView(
        'enable_banking.retrieve_session.select_session',
        'account_statement_enable_banking.'
        'enable_banking_retrieve_session_select_form',
        [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Create New Session', 'create_session', 'tryton-export'),
            Button('Use Existing Session', 'use_session', 'tryton-refresh', default=True),
        ])
    create_session = StateAction(
        'account_statement_enable_banking.url_session')
    use_session = StateTransition()

    def transition_check_session_before_start(self):
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
            if eb_session.session and not eb_session.session_expired:
                session = json.loads(eb_session.session)
                r = requests.get(
                    f"{URL}/sessions/{session['session_id']}",
                    headers=base_headers)
                if r.status_code == 200:
                    session = r.json()
                    if session['status'] == 'AUTHORIZED':
                        return 'end'
            if eb_session:
                EBSession.delete([eb_session])
        return 'start'

    def default_start(self, fields):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        Date = pool.get('ir.date')

        active_id = Transaction().context.get('active_id', None)
        journal = Journal(active_id) if active_id else None
        if not journal or not journal.bank_account:
            raise AccessError(gettext(
                    'account_statement_enable_banking.msg_no_bank_account'))

        valid = (
            journal.enable_banking_session.valid_until.date() >= Date.today()
            if (journal.enable_banking_session
                and journal.enable_banking_session.valid_until)
            else False)

        return {
            'enable_banking_session_valid': valid,
            }

    def transition_check_session(self):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        EBSession = pool.get('enable_banking.session')

        active_id = Transaction().context.get('active_id', None)
        journal = Journal(active_id) if active_id else None
        if not journal or not journal.bank_account:
            raise AccessError(gettext(
                    'account_statement_enable_banking.msg_no_bank_account'))

        # We need to check the date and if we have the field session, if
        # not, the session was not created correctly and need to be deleted
        eb_session = journal.enable_banking_session
        if eb_session and eb_session.session and eb_session.session_expired:
            EBSession.delete([eb_session])

        eb_sessions = EBSession.search([
            ('bank', '=', journal.bank_account.bank),
            ])
        eb_session = [ebs for ebs in eb_sessions
            if ebs.session_expired is False]
        if eb_session:
            return 'select_session'
        return 'create_session'

    def default_select_session(self, fields):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        EBSession = pool.get('enable_banking.session')

        active_id = Transaction().context.get('active_id', None)
        journal = Journal(active_id) if active_id else None
        if not journal or not journal.bank_account:
            return None
        eb_sessions = EBSession.search([
            ('bank', '=', journal.bank_account.bank),
            ])
        eb_session = [ebs for ebs in eb_sessions
            if ebs.session_expired is False]

        return {
            'found_session': eb_session[0].id if eb_session else None,
            }

    def transition_use_session(self):
        pool = Pool()
        Journal = pool.get('account.statement.journal')

        active_id = Transaction().context.get('active_id', None)
        journal = Journal(active_id) if active_id else None
        eb_session = self.select_session.found_session
        journal.enable_banking_session = eb_session
        journal.on_change_enable_banking_session()
        journal.save()
        return 'end'

    def do_create_session(self, action):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        EBSession = pool.get('enable_banking.session')

        journal_id = Transaction().context.get('active_id', None)
        journal = Journal(journal_id) if journal_id else None
        if not journal or not journal.bank_account:
            raise AccessError(gettext(
                    'account_statement_enable_banking.msg_no_bank_account'))
        enable_banking_session_valid_days = (
            self.start.enable_banking_session_valid_days)
        base_headers = get_base_header()
        if not journal.aspsp_name or not journal.aspsp_country:
            bank_name = journal.bank_account.bank.party.name.lower()
            bic = (journal.bank_account.bank.bic or '').lower()
            if journal.bank_account.bank.party.addresses:
                country = (
                    journal.bank_account.bank.party.addresses[0].country.code)
            else:
                raise AccessError(gettext('account_statement_enable_banking.'
                        'msg_no_country'))

            if (enable_banking_session_valid_days < timedelta(days=1)
                    or enable_banking_session_valid_days > timedelta(
                        days=180)):
                raise AccessError(
                    gettext('account_statement_enable_banking.'
                        'msg_valid_days_out_of_range'))

            # We fill the aspsp name and country using the bank account
            r = requests.get(f"{URL}/aspsps", headers=base_headers)
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
        eb_session.aspsp_name = journal.aspsp_name
        eb_session.aspsp_country = journal.aspsp_country
        eb_session.bank = journal.bank_account.bank
        eb_session.session_id = token_hex(16)
        eb_session.valid_until = (
            datetime.now() + enable_banking_session_valid_days)
        EBSession.save([eb_session])
        body = {
            'access': {
                'valid_until': (datetime.now(UTC)
                    + enable_banking_session_valid_days).isoformat(),
                },
            'aspsp': {
                'name': journal.aspsp_name,
                'country': journal.aspsp_country,
                },
            'state': eb_session.session_id,
            'redirect_url': REDIRECT_URL,
            'psu_type': 'personal',
        }

        r = requests.post(f"{URL}/auth", json=body, headers=base_headers)
        if r.status_code == 200:
            action['url'] = r.json()['url']
        else:
            raise AccessError(
                gettext('account_statement_enable_banking.'
                    'msg_error_create_session',
                    error_code=r.status_code,
                    error_message=r.text))
        journal.enable_banking_session = None
        journal.save()
        return action, {}


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
        if not PRODUCTION:
            return []
        for journal in Journal.search([
                ('company.id', '=', company_id),
                ('synchronize_journal', '=', True)
                ]):
            eb_session = journal.enable_banking_session
            if (eb_session is None or (eb_session and (
                            eb_session.session is None or (
                                eb_session.valid_until
                                and eb_session.session_expired)))):
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
