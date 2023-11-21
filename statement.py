# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import requests
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from secrets import token_hex
from itertools import groupby
from unidecode import unidecode
import re
from sql.functions import Function
from trytond.model import Workflow, ModelView, ModelSQL, fields, tree
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval, Bool, If
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


_ZERO = Decimal('0.0')


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
    def cancel(cls, statements):
        pool = Pool()
        Origin = pool.get('account.statement.origin')

        origins = [o for s in statements for o in s.origins]
        Origin.cancel(origins)

        super().cancel(statements)


class Line(metaclass=PoolMeta):
    __name__ = 'account.statement.line'

    suggested_line = fields.Many2One('account.statement.origin.suggested.line',
        'Suggested Lines', ondelete="RESTRICT")

    @classmethod
    def __setup__(cls):
        super().__setup__()

        cls.related_to.domain['account.move.line'] = [
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
            ['OR',
                ('move_origin', '=', None),
                ('move.origin', 'not like', 'account.invoice,%'),
                ],
            ]

    @classmethod
    def _get_relations(cls):
        return super()._get_relations() + ['account.move.line']

    @fields.depends('origin', '_parent_origin.second_currency')
    def on_change_with_second_currency(self, name=None):
        if self.origin and self.origin.second_currency:
            return self.origin.second_currency

    @fields.depends('origin', '_parent_origin.amount_second_currency')
    def on_change_with_amount_second_currency(self, name=None):
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

    @fields.depends('party', methods=['move_line'])
    def on_change_related_to(self):
        super().on_change_related_to()
        if self.move_line:
            if not self.party:
                self.party = self.move_line.party
            self.account = self.move_line.account

    @classmethod
    def cancel_move(cls, lines):
        pool = Pool()
        Move = pool.get('account.move')
        MoveLine = pool.get('account.move.line')
        Reconciliation = pool.get('account.move.reconciliation')
        Invoice = pool.get('account.invoice')

        for line in lines:
            move = line.move
            if move:
                to_unreconcile = [x.reconciliation for x in move.lines
                    if x.reconciliation]
                if to_unreconcile:
                    to_unreconcile = Reconciliation.browse([
                            x.id for x in to_unreconcile])
                    Reconciliation.delete(to_unreconcile)

                # On possible realted invoices, need to unlink the payment
                # lines
                to_unpay = [x for x in move.lines if x.invoice_payment]
                if to_unpay:
                    Invoice.remove_payment_lines(to_unpay)

                cancel_move = move.cancel()
                cancel_move.origin = line.origin
                Move.post([cancel_move])
                mlines = [l for m in [move, cancel_move]
                    for l in m.lines if l.account.reconcile]
                if mlines:
                    MoveLine.reconcile(mlines)

    @classmethod
    def cancel_lines(cls, lines):
        '''As is needed save an history fo all movements, do not remove the
        possible move related. Create the cancelation move and leave they
        related to the statement and the origin, to have an hstory.
        '''
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        SuggestedLine = pool.get('account.statement.origin.suggested.line')
        Warning = pool.get('res.user.warning')

        moves = []
        mlines = []
        for line in lines:
            if line.move:
                warning_key = Warning.format(
                    'origin_line_with_move', [line.move.id])
                if Warning.check(warning_key):
                    raise StatementValidateWarning(warning_key,
                        gettext('account_statement_enable_banking.'
                            'msg_origin_line_with_move',
                            move=line.move.rec_name,
                            ))
                for mline in line.move.lines:
                    if mline.origin == line:
                        mline.origin = line.origin
                        mlines.append(mline)
                moves.append(line.move)
        if mlines:
            with Transaction().set_context(from_account_statement_origin=True):
                MoveLine.save(mlines)
        cls.cancel_move(lines)

        suggested_lines = [x.suggested_line for x in lines
            if x.suggested_line]
        suggested_lines.extend(list(set([x.parent
                        for x in suggested_lines if x.parent])))
        if suggested_lines:
            SuggestedLine.propose(suggested_lines)

    @classmethod
    def delete(cls, lines):
        cls.cancel_lines(lines)
        super().delete(lines)

    @classmethod
    def delete_move(cls, lines):
        cls.cancel_lines(lines)
        super().delete_move(lines)


class Origin(Workflow, metaclass=PoolMeta):
    __name__ = 'account.statement.origin'

    entry_reference = fields.Char("Entry Reference", readonly=True)
    suggested_lines = fields.One2Many(
        'account.statement.origin.suggested.line', 'origin',
        'Suggested Lines')
    suggested_lines_tree = fields.Function(
        fields.Many2Many('account.statement.origin.suggested.line', None, None,
            'Suggested Lines'), 'get_suggested_lines_tree')
    state = fields.Selection([
            ('registered', "Registered"),
            ('cancelled', "Cancelled"),
            ('posted', "Posted"),
            ], "State", readonly=True, sort=False)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.number.search_unaccented = False
        cls._order.insert(0, ('date', 'ASC'))
        cls._order.insert(1, ('number', 'ASC'))
        cls.lines.states['readonly'] |= (
            (Eval('state') != 'registered')
            )
        cls._transitions |= set((
                ('registered', 'posted'),
                ('registered', 'cancelled'),
                ('cancelled', 'registered'),
                ('posted', 'cancelled'),
                ))
        cls._buttons.update({
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

    @fields.depends('statement', 'lines', 'company',
        '_parent_statement.company', '_parent_statement.journal')
    def on_change_lines(self):
        if (not self.statement or not self.statement.journal
                or not self.statement.company):
            return
        if self.statement.journal.currency != self.statement.company.currency:
            return

        invoices = set()
        payments = set()
        move_lines = set()
        for line in self.lines:
            if (line.invoice
                    and line.invoice.currency == self.company.currency):
                invoices.add(line.invoice)
            if (line.payment
                    and line.payment.currency == self.company.currency):
                payments.add(line.payment)
            if (line.move_line
                    and line.move_line.currency == self.company.currency):
                move_lines.add(line.move_line)
        invoice_id2amount_to_pay = {}
        for invoice in invoices:
            if invoice.type == 'out':
                sign = -1
            else:
                sign = 1
            invoice_id2amount_to_pay[invoice.id] = sign * invoice.amount_to_pay

        payment_id2amount = (dict((x.id, x.amount) for x in payments)
            if payments else {})

        move_line_id2amount = (dict((x.id, x.amount) for x in move_lines)
            if move_lines else {})

        lines = list(self.lines)
        for line in lines:
            if (line.invoice
                    and line.id
                    and line.invoice.id in invoice_id2amount_to_pay):
                amount_to_pay = invoice_id2amount_to_pay[line.invoice.id]
                if (amount_to_pay
                        and getattr(line, 'amount', None)
                        and (line.amount >= 0) == (amount_to_pay <= 0)):
                    if abs(line.amount) > abs(amount_to_pay):
                        line.amount = amount_to_pay.copy_sign(line.amount)
                    else:
                        invoice_id2amount_to_pay[line.invoice.id] = (
                            line.amount + amount_to_pay)
                else:
                    line.invoice = None
            if (line.payment
                    and line.id
                    and line.payment.id in payment_id2amount):
                amount = payment_id2amount[line.payment.id]
                if amount and getattr(line, 'amount', None):
                    if abs(line.amount) > abs(amount):
                        line.amount = amount.copy_sign(line.amount)
                    else:
                        payment_id2amount[line.payment.id] = (
                            line.amount + amount)
                else:
                    line.payment = None
            if (line.move_line
                    and line.id
                    and line.move_line.id in move_line_id2amount):
                amount = move_line_id2amount[line.move_line.id]
                if amount and getattr(line, 'amount', None):
                    if abs(line.amount) > abs(amount):
                        line.amount = amount.copy_sign(line.amount)
                    else:
                        move_line_id2amount[line.move_line.id] = (
                            line.amount + amount)
                else:
                    line.move_line = None
        self.lines = lines

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
        Warning = pool.get('res.user.warning')

        paid_cancelled_invoice_lines = []
        for origin in origins:
            origin.validate_amount()
            paid_cancelled_invoice_lines.extend(x for x in origin.lines
                if x.invoice and x.invoice.state in {'cancelled', 'paid'})

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
        Move = pool.get('account.move')
        MoveLine = pool.get('account.move.line')

        moves = []
        for origin in origins:
            for key, lines in groupby(
                    origin.lines, key=origin.statement._group_key):
                lines = list(lines)
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
            for line in lines:
                move_line = line.get_move_line()
                if not move_line:
                    continue
                move_line.move = move
                amount += move_line.debit - move_line.credit
                if move_line.amount_second_currency:
                    amount_second_currency += move_line.amount_second_currency
                move_lines.append((move_line, line))

            move_line = origin.statement._get_move_line(
                amount, amount_second_currency, lines)
            move_line.move = move
            move_lines.append((move_line, None))

        MoveLine.save([x for x, _ in move_lines])
        StatementLine.reconcile(move_lines)
        return moves

    @classmethod
    @ModelView.button
    @Workflow.transition('registered')
    def register(cls, origins):
        pass

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
        # It's an awfull hack to sate the state, but it's needed to ensure the
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

        statements = []
        for origin in origins:
            statement = origin.statement
            try:
                getattr(statement, 'validate_%s' % statement.validation)()
                statements.append(origin.statement)
            except StatementValidateError:
                pass
        if statements:
            Statement.post(statements)

    @classmethod
    @ModelView.button
    @Workflow.transition('cancelled')
    def cancel(cls, origins):
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        Statement = pool.get('account.statement')
        StatementLine = pool.get('account.statement.line')
        Warning = pool.get('res.user.warning')

        lines = [x for o in origins for x in o.lines]
        moves = dict((x.move, x.origin) for x in lines if x.move)
        if moves:
            warning_key = Warning.format('cancel_origin_line_with_move',
                list(moves.keys()))
            if Warning.check(warning_key):
                raise StatementValidateWarning(warning_key,
                    gettext('account_statement_enable_banking.'
                        'msg_cancel_origin_line_with_move',
                        moves=", ".join(m.rec_name for m in moves)))
            StatementLine.cancel_move(lines)
            with Transaction().set_context(from_account_statement_origin=True):
                to_write = []
                for move, origin in moves.items():
                    for line in move.lines:
                        if line.origin in origin.lines:
                            to_write.extend(([line], {'origin': origin}))
                if to_write:
                    MoveLine.write(*to_write)
        StatementLine.write(lines, {'move': None})

    def similarity_parties(self, compare):
        """
        This function return a dictionary with the possible parties ID on
        'key' and the similairty on 'value'.
        It compare the 'compare' value with the parties name, based on the
        similarities journal deffined values.
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
            where = ((similarity_party >= self.similarity_threshold) | (
                    (party_table.trade_name != None)
                    & (similarity_party_trade >= self.similarity_threshold)))
        else:
            where = (similarity_party >= self.similarity_threshold)
        query = party_table.select(party_table.id, similarity_party,
            where=where)
        cursor.execute(*query)
        for similarity in cursor.fetchall():
            similarity_parties[similarity[0]] = round(similarity[1] * 10)
        return similarity_parties

    def increase_similarity_by_interval_date(self, date, interval_date=None,
            similarity=0):
        """
        This funtion increase the similarity if the dates are equal or in the
        interval.
        """
        if date:
            control_date = self.date
            if not interval_date:
                interval_date = timedelta(days=3)
            start_date = control_date - interval_date
            end_date = control_date + interval_date
            if date == control_date:
                similarity += 2
            elif start_date <= date <= end_date:
                similarity += 1
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
                    similarity += 2
                else:
                    similarity += 1
        return similarity

    def create_payment_suggested_line(self, move_lines, amount, name,
            payment=False, similarity=0):
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
            if payment and line.payments:
                if not parent and not name:
                    name = line.payments[0].rec_name
                related_to = line.payments[0]
            else:
                related_to = line.move_origin if line.move_origin else line
                if line.second_currency != self.currency:
                    second_currency = line.second_currency
                    amount_second_currency = line.amount_second_currency
                if not parent and not name:
                    name = line.rec_name
            amount_line = line.debit - line.credit
            values = {
                'name': '' if parent else name,
                'parent': parent,
                'origin': self,
                'party': line.party,
                'date': line.maturity_date,
                'related_to': related_to,
                'account': line.account,
                'amount': amount_line,
                'second_currency': self.second_currency,
                'amount_second_currency': self.amount_second_currency,
                'similarity': similarity,
                'state': 'proposed',
                }
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
        second_currency = self.second_currency
        amount_second_currency = self.amount_second_currency
        for line in move_lines:
            related_to = line.move_origin if line.move_origin else line
            if line.second_currency != self.currency:
                second_currency = line.second_currency
                amount_second_currency = line.amount_second_currency
            amount_line = line.debit - line.credit
            values = {
                'name': '' if parent else name,
                'parent': parent,
                'origin': self,
                'party': line.party,
                'date': line.maturity_date,
                'related_to': related_to,
                'account': line.account,
                'amount': amount_line,
                'second_currency': second_currency,
                'amount_second_currency': amount_second_currency,
                'similarity': similarity,
                'state': 'proposed',
                }
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
            key = (group, payment_date)
            if key in groups['groups']:
                groups['groups'][key]['amount'] += payment_amount
                groups['groups'][key]['payments'].append(payment)
            else:
                groups['groups'][key] = {
                    'amount': payment_amount,
                    'payments': [payment],
                    }
        if groups['amount'] == abs(amount) and len(groups['groups']) > 1:
            name = gettext('account_statement_enable_banking.msg_payments')
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
                if vals['amount'] == abs(amount):
                    similarity = self.increase_similarity_by_interval_date(
                        date, similarity=acceptable)
                    lines.extend([x.line for x in vals['payments']])
                    # Only check the party similarity if have one payment
                    if len(groups['groups']) == 1 and len(lines) == 1:
                        party = vals['payments'][0].party
                        if party and parties:
                            similarity = self.increase_similarity_by_party(
                                party, parties, similarity=similarity)
                    parent, to_create = self.create_payment_suggested_line(
                        lines, amount, name=group.rec_name,
                        similarity=similarity)
                    suggesteds.extend(to_create)
            move_lines.extend(lines)
        return suggesteds, move_lines

    def _search_move_line_reconciliation_domain(self, exclude_ids=None):
        domain = [
            ('move.company', '=', self.company.id),
            ('currency', '=', self.currency),
            ('move_state', '=', 'posted'),
            ('reconciliation', '=', None),
            ('account.reconcile', '=', True),
            ['OR',
                ('account.type.receivable', '=', True),
                ('account.type.payable', '=', True)
                ],
            ['OR',
                ('origin', '=', None),
                ('origin', 'like', 'account.invoice,%')
                ],
            #['OR',
            #    ('origin', '=', None),
            #    ('origin', 'not like', 'account.invoice.tax,%')
            #    ('origin', 'not like', 'account.bank.statements,%')
            #    ],
            ]
        if exclude_ids:
            domain.append(('id', 'not in', exclude_ids))
        return domain

    def _search_suggested_reconciliation_move_line(self, amount, acceptable=0,
            parties=None, exclude=None):
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
        domain = self._search_move_line_reconciliation_domain(line_ids)
        lines_by_origin = {}
        lines_by_party = {}
        for line in MoveLine.search(domain, order=[('maturity_date', 'ASC')]):
            move_amount = line.debit - line.credit
            if move_amount == amount:
                similarity = self.increase_similarity_by_interval_date(
                    line.maturity_date, similarity=acceptable)
                party = line.party
                if party and parties:
                    similarity = self.increase_similarity_by_party(party,
                        parties, similarity=similarity)
                name = line.origin.rec_name if line.origin else (party.rec_name
                    if party else None)
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
                    if line_partes.count(line_parties[0]) == len(line_parties)
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
                parent, to_create = self.create_move_suggested_line(
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
                parent, to_create = self.create_move_suggested_line(
                    values['lines'], amount, name=name, similarity=similarity)
                suggesteds.extend(to_create)
        return suggesteds

    def _search_suggested_reconciliation_simlarity(self, amount,
            information=None, threshold=0, acceptable=0):
        """
        Search for old origins lines. Reproducing the same line/s created.
        """
        pool = Pool()
        Origin = pool.get('account.statement.origin')
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        origin_table = pool.get('account.statement.origin').__table__()
        line_table = pool.get('account.statement.line').__table__()
        cursor = Transaction().connection.cursor()

        suggesteds = []

        if not amount or not information:
            return suggesteds

        similarity_column = Similarity(JsonbExtractPathText(
                origin_table.information, 'remittance_information'),
            information)
        query = origin_table.join(line_table,
            condition=origin_table.id == line_table.move).select(
            origin_table.id, similarity_column,
            where=((similarity_column >= threshold)
                & (origin_table.state == 'posted')
                & (line_table.related_to == None))
                )
        cursor.execute(*query)
        name = gettext('account_statement_enable_banking.msg_similarity')
        for origins in cursor.fetchall():
            origin, = Origin.browse([origins[0]])
            suggests = []
            for line in origin.lines:
                values = {
                    'name': name,
                    'parent': None,
                    'origin': self,
                    'party': line.party,
                    'date': self.date,
                    'account': line.account,
                    'amount': line.amount,
                    'second_currency': self.second_currency,
                    'amount_second_currency': self.amount_second_currency,
                    'similarity_threshold': origins[1],
                    'state': 'proposed'
                    }
                suggests.append(values)
            if len(suggests) == 1:
                suggests[0]['amount'] = amount
            elif len(suggests) > 1:
                parent = SuggestedLine()
                parent.origin = self
                parent.name = name
                parent.amount = amount
                parent.state = 'proposed'
                parent.similarity_threshold = origins[1]
                parent.save()
                for suggest in suggests:
                    suggest['parent'] = parent
                    suggest['name'] = ''
            suggesteds.extend(suggests)
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
                raise AccessError(
                    gettext('account_statement_enable_banking.'
                        'msg_suggested_line_related_to_statement_line'))
            SuggestedLine.delete(suggests)

        suggesteds_to_create = []
        for origin in origins:
            pending_amount = origin.pending_amount
            if pending_amount == _ZERO:
                return

            information = origin.information.get('remittance_information',
                None)
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
                        pending_amount, acceptable=acceptable,
                        parties=similarity_parties))
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
            move_lines.extend(used_move_lines)

            # Search by simlarity, using the PostreSQL Trigram
            suggesteds_to_create.extend(
                origin._search_suggested_reconciliation_simlarity(
                        pending_amount, information=information,
                        threshold=threshold, acceptable=acceptable))

        suggesteds_use = []
        to_save = []
        if suggesteds_to_create:
            # Remove duplicate suggestions
            suggesteds_to_create = list(set(
                    [tuple(x.items()) for x in suggesteds_to_create]))
            suggesteds_to_create = [dict(x) for x in suggesteds_to_create]
            SuggestedLine.create(suggesteds_to_create)
            for origin in origins:
                suggested_use = None
                before_similarity = 0.0
                for suggest in SuggestedLine.search([
                        ('origin', '=', origin),
                        ('parent', '=', None),
                        ('similarity', '>=', acceptable)
                        ]):
                    if suggest.similarity == before_similarity:
                        suggested_use = None
                        break
                    elif suggest.similarity < before_similarity:
                        break
                    suggested_use = suggest
                    before_similarity = suggest.similarity
                if suggested_use:
                    # Set the origin second_currency in case the line or lines
                    # finded have a different second currency
                    if suggested_use.second_currency != origin.second_currency:
                        origin.second_currency = suggested_use.second_currency
                        origin.amount_second_currency = (
                            suggested_use.amount_second_currency)
                        to_save.append(origin)
                    suggesteds_use.append(suggested_use)
        if to_save:
            cls.save(to_save)
        if suggesteds_use:
            SuggestedLine.use(suggesteds_use)

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
        super().delete(origins)


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
                ['OR',
                    ('move_origin', '=', None),
                    ('move.origin', 'not like', 'account.invoice,%'),
                    ],
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
                description = child.origin.information.get(
                    'remittance_information', '')
                values = {
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
                    'description': description,
                    }
                to_create.append(values)
            if len(childs) > 1:
                cls.write(list(childs), {'state': 'used'})
        StatementLine.create(to_create)


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

        journal = Journal(Transaction().context['active_id'])
        if not journal.bank_account:
            raise AccessError(gettext(
                    'account_statement_enable_banking.msg_no_bank_account'))

        eb_sessions = EBSession.search([
            ('company', '=', journal.company.id),
            ('bank', '=', journal.bank_account.bank.id)], limit=1)
        if eb_sessions:
            # We need to check the date and if we have the field session, if
            # not the session was not created correctly and need to be deleted
            eb_session = eb_sessions[0]
            if eb_session.session:
                session = eval(eb_session.session)
                r = requests.get(
                    f"{config.get('enable_banking', 'api_origin')}"
                    f"/sessions/{session['session_id']}",
                    headers=base_headers)
                if r.status_code == 200:
                    session = r.json()
                    if (session['status'] == 'AUTHORIZED' and
                            datetime.now() < eb_session.valid_until and
                            eb_session.session):
                        return 'sync_statements'
            EBSession.delete(eb_sessions)
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
        aspsp_found = False
        for aspsp in r.json()["aspsps"]:
            if aspsp["country"] != country:
                continue
            if (aspsp["name"].lower() == bank_name
                    or aspsp.get("bic", " ").lower() == bic):
                journal.aspsp_name = aspsp["name"]
                journal.aspsp_country = aspsp["country"]
                Journal.save([journal])
                aspsp_found = True
                break

        if not aspsp_found:
            raise AccessError(
                gettext('account_statement_enable_banking.msg_aspsp_not_found',
                    bank=journal.aspsp_name,
                    country_code=journal.aspsp_country))

        eb_session = EBSession()
        eb_session.company = journal.company
        eb_session.aspsp_name = journal.aspsp_name
        eb_session.aspsp_country = journal.aspsp_country
        eb_session.bank = journal.bank_account.bank
        eb_session.session_id = token_hex(16)
        eb_session.valid_until = datetime.fromtimestamp(
            int(datetime.now().timestamp()) + 86400)
        EBSession.save([eb_session])
        base_headers = get_base_header()
        body = {
            'access': {'valid_until': (
                datetime.now(timezone.utc) + timedelta(days=10)).isoformat()},
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
        return action, {}

    def transition_sync_statements(self):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        journal = Journal(Transaction().context['active_id'])
        journal.synchronize_statements_enable_banking()
        return 'end'


class Cron(metaclass=PoolMeta):
    __name__ = 'ir.cron'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.method.selection.extend([
            ('account.statement.journal|synchronize_enable_banking_journals',
                "Synchronize Enable Banking Journals"),
            ])
