# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import requests
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from secrets import token_hex
from itertools import groupby
from unidecode import unidecode
import re

from trytond.model import Workflow, ModelView, ModelSQL, fields, tree
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval, Id, Bool, If
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


class Statement(metaclass=PoolMeta):
    __name__ = 'account.statement'

    @classmethod
    def cancel(cls, statements):
        pool = Pool()
        Origin = pool.get('account.statement.origin')

        origins = [s.origin for s in statements]
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
                    #to_unpay = [MoveLine(x.id) for x in to_unpay]
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

        amount = sum(l.amount for l in self.lines)
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
            paid_cancelled_invoice_lines.extend(l for l in origin.lines
                if l.invoice and l.invoice.state in {'cancelled', 'paid'})

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

        MoveLine.save([l for l, _ in move_lines])
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
        Move = pool.get('account.move')
        MoveLine = pool.get('account.move.line')

        cls.validate_origin(origins)
        cls.create_moves(origins)

        lines = [l for o in origins for l in o.lines]
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

    def check_key_value_exists(self, key, value, dict_list):
        """Checks if a key exists and have a specifica value in a list of
        dictionaries.
        """
        for d in dict_list:
            if d.get(key, None) == value:
                return True
        return False

    def create_suggested_line(self, move_lines, amount, name=None,
            payment=False):
        """
        Create one or more suggested registers based on the move_lines.
        If there are more than one move_line, it will be grouped.
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
            parent.amount = amount
            parent.state = 'proposed'
            parent.save()

        second_currency = self.second_currency
        amount_second_currency = self.amount_second_currency
        for line in move_lines:
            if line.move_origin and isinstance(line.move_origin, Invoice):
                invoice = line.move_origin
                if not name:
                    name = invoice.rec_name
                related_to = invoice
                if (second_currency is None
                        and invoice.currency != self.currency):
                    second_currency = invoice.currency
                    amount_second_currency = (invoice.total_amount
                        if invoice.type == 'out'
                        else -1 * invoice.total_amount)
            elif payment and line.payments:
                if not name:
                    name = line.payments[0].rec_name
                related_to = line.payments[0]
            else:
                if not name:
                    name = line.rec_name
                related_to = line
            if parent and parent.name is None:
                parent.name = name
                parent.save()
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
                'state': 'proposed'
                }
            to_create.append(values)
        return parent, to_create

    def _search_move_line_reconciliation_domain(self):
        return [
            ('move.company', '=', self.company.id),
            ('currency', '=', self.currency),
            ('move_state', '=', 'posted'),
            ('reconciliation', '=', None),
            ('account.reconcile', '=', True),
            ]

    def _match_parties(self, text, domain=[]):
        pool = Pool()
        Party = pool.get('party.party')

        match_parties = []
        text = unidecode(text).upper()

        def check_match_party(name):
            if name in text and party not in match_parties:
                match_parties.append(party)

        for party in Party.search(domain):
            name = unidecode(party.name).upper()
            check_match_party(name)
            name = name.replace(',', '').replace('.', '')
            check_match_party(name)
            # Try to remove the most used type of comapny at the end
            patern = r'\s(SL|SA|SLU|SAU|COOP|SLL|SAL|SLP|SLNE|SC|SCCL|INC'\
                '|LLC|CO)$'
            name = re.sub(patern, '', name, count=1)
            name = name.strip()
            check_match_party(name)
            if hasattr(party, 'trade_name') and party.trade_name:
                name = unidecode(party.trade_name).upper()
                check_match_party(name)
                name = name.replace(',', '').replace('.', '')
                check_match_party(name)
        return match_parties

    def _search_suggested_reconciliation_by_party(self):
        """
        Search for the possible move line with the party that could be
        realted to this Origin, and add all them in the suggested field.
        Return a list with suggested lines to save, the possible suggested
        line to use as default, the move lines used and the parties found
        in Origin information, but not found a move line.
        """
        pool = Pool()
        MoveLine = pool.get('account.move.line')

        suggesteds = []
        suggested_used = None
        move_lines_used = []

        # search only for the same ammount and possible party
        pending_amount = self.pending_amount
        if pending_amount == _ZERO:
            return suggesteds, suggested_used

        # Prepapre the base domain
        domain = self._search_move_line_reconciliation_domain()

        # Get from account statement origin the possible party name download
        # from the Bank. In Spain this information is setted in the field
        # called remittance_information. In this fild are more information,
        # but one of the possible information is the aprty name.
        remittance_information = self.information.get(
            'remittance_information', None)

        # If exist the 'remittance_information' field it may be contain the
        # name of the party which recive or do the payment.
        # Prepare a list with the possible parties that match with the possible
        # text in the 'remittance_information' field.
        match_parties = []
        if remittance_information:
            match_parties = self._match_parties(remittance_information)

        # Check if the possible matched names and the default information
        # found any move line.
        # TODO: Not control the amount, to try to find a possible invoice/s
        # and credit note/s.
        # TODO: Maybe use the algorithm in account_reconcile module used to
        # reconcile multiples lines separated in time.
        used_parties = []
        for party in match_parties:
            party_lines = MoveLine.search(domain + [('party', '=', party)],
                order=[('maturity_date', 'ASC')])
            if party_lines:
                used_parties.append(party)
                lines_by_origin = {}
                for line in party_lines:
                    amount = line.debit - line.credit
                    if line.move_origin and line.move_origin in lines_by_origin:
                        lines_by_origin[line.move_origin]['amount'] += amount
                        lines_by_origin[line.move_origin]['lines'].append(line)
                    elif line.move_origin:
                        lines_by_origin[line.move_origin] = {
                            'amount': amount,
                            'lines': [line]
                                }
                    if amount == pending_amount:
                        parent, to_create = self.create_suggested_line([line],
                            pending_amount)
                        suggested_used = to_create[0]['name']
                        suggesteds.extend(to_create)
                # Check if there are more than one move from the same origin
                # that sum the pending_amount
                for values in lines_by_origin.values():
                    if (len(values['lines']) > 1
                            and values['amount'] == pending_amount):
                        parent, to_create = self.create_suggested_line(
                            values['lines'], pending_amount)
                        if not suggested_used:
                            suggested_used = parent.name
                        suggesteds.extend(to_create)
            move_lines_used.extend(party_lines)
        parties = list(set(match_parties) - set(used_parties))
        return suggesteds, suggested_used, move_lines_used, parties

    def _search_suggested_reconciliation_by_invoice(self, exclude=None):
        """
        Search for the possible move line with invoice as origin and without
        party that could be realted to this Origin, and add all them in the
        suggested field.
        """
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        InvoiceTax = pool.get('account.invoice.tax')

        suggesteds = []
        suggested_used = None

        pending_amount = self.pending_amount
        if pending_amount == _ZERO:
            return suggesteds, suggested_used

        # Prepapre the base domain
        domain = self._search_move_line_reconciliation_domain()
        domain.append(('move_origin', 'like', 'account.invoice,%'))
        if exclude and isinstance(exclude, list):
            domain.append(('id', 'not in', [x.id for x in exclude]))

        lines_by_origin = {}
        for line in MoveLine.search(domain, order=[('maturity_date', 'ASC')]):
            if isinstance(line.origin, InvoiceTax):
                continue
            amount = line.debit - line.credit
            if line.move_origin in lines_by_origin:
                lines_by_origin[line.move_origin]['amount'] += amount
                lines_by_origin[line.move_origin]['lines'].append(line)
            else:
                lines_by_origin[line.move_origin] = {
                    'amount': amount,
                    'lines': [line]
                        }
        for origin, values in lines_by_origin.items():
            if pending_amount == values['amount']:
                parent, to_create = self.create_suggested_line(
                    values['lines'], pending_amount)
                if not suggested_used:
                    suggested_used = (parent.name if parent
                        else to_create[0]['name'])
                suggesteds.extend(to_create)

        return suggesteds, suggested_used

    def _search_payment_group_reconciliation_domain(self, amount, kind):
        return [
            ('journal.currency', '=', self.currency),
            ('kind', '=', kind),
            ('total_amount', '=', amount),
            ('company', '=', self.company.id),
            ]

    def _search_suggested_reconciliation_payment_group(self, exclude=None):
        pool = Pool()
        Group = pool.get('account.payment.group')

        suggesteds = []
        suggested_used = None
        groups = []

        pending_amount = self.pending_amount
        if pending_amount == _ZERO:
            return suggesteds, suggested_used, groups

        kind = 'receivable' if pending_amount > _ZERO else 'payable'
        domain = self._search_payment_group_reconciliation_domain(
            abs(pending_amount), kind)

        for group in Group.search(domain):
            if group.journal.clearing_account is None:
                continue
            found = True
            for payment in group.payments:
                if (payment.state == 'failed' or (payment.line
                            and payment.state != 'failed'
                            and payment.line.reconciliation)):
                    found = False
                    break
            if found:
                groups.append(group)

        for group in groups:
            name = group.rec_name
            values = {
                'name': name,
                'origin': self,
                'date': group.planned_date,
                'related_to': group,
                'amount': pending_amount,
                'second_currency': self.second_currency,
                'state': 'proposed'
                }
            suggesteds.append(values)
            if not suggested_used:
                suggested_used = name
        return suggesteds, suggested_used, groups

    def _search_payment_reconciliation_domain(self):
        return [
            ('currency', '=', self.currency),
            ('company', '=', self.company.id),
            ]

    def _search_suggested_reconciliation_payment(self, exclude=None):
        pool = Pool()
        Payment = pool.get('account.payment')
        Group = pool.get('account.payment.group')
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        suggesteds = []
        suggested_used = None

        pending_amount = self.pending_amount
        sign = -1 if pending_amount < _ZERO else 1
        if pending_amount == _ZERO:
            return suggesteds, suggested_used

        kind = 'receivable' if pending_amount > _ZERO else 'payable'
        domain = self._search_payment_reconciliation_domain()

        payment_groups = {
            'amount': _ZERO,
            'groups': {}
            }
        payment_groups_clearing = {
            'amount': _ZERO,
            'groups': {}
            }
        groups = []
        # TODO: Ensure what to do when payment is "exit"
        for payment in Payment.search(domain):
            #if payment.group and payment.group in groups:
            #    continue
            if (payment.line and payment.state != 'failed'
                    and payment.line.reconciliation is None):
                amount = payment.amount
                if payment.group in exclude:
                    continue
                if (not payment.journal
                        or payment.journal.clearing_account is None):
                    payment_groups['amount'] += amount
                    group = payment.group if payment.group else payment
                    if group in groups:
                        payment_groups['groups'][group]['amount'] += amount
                        payment_groups['groups'][group]['payments'].append(
                            payment)
                    else:
                        payment_groups['groups'][group] = {
                            'amount': amount,
                            'payments': [payment],
                            }
                        groups.append(group)
                else:
                    payment_groups_clearing['amount'] += amount
                    group = payment.group if payment.group else payment
                    if group in groups:
                        payment_groups_clearing['groups'][group][
                            'amount'] += amount
                        payment_groups_clearing['groups'][group][
                            'payments'].append(payment)
                    else:
                        payment_groups_clearing['groups'][group] = {
                            'amount': amount,
                            'payments': [payment],
                            }
                        groups.append(group)

        if payment_groups['amount'] == abs(pending_amount):
            name = gettext("Payments")
            if len(payment_groups['groups']) == 1:
                key = payment_groups['group'].keys()[0]
                if len(payment_groups['groups'][key]['payments']) == 1:
                    name = None
            lines = [p.line for v in payment_groups['groups'].values()
                for p in v['payments']]
            parent, to_create = self.create_suggested_line(lines,
                pending_amount, name=name)
            if not suggested_used:
                suggested_used = (parent.name if parent
                    else to_create[0]['name'])
            suggesteds.extend(to_create)
        elif payment_groups['amount'] != _ZERO:
            for group, vals in payment_groups['groups'].items():
                if vals['amount'] == abs(pending_amount):
                    lines = [x.line for x in vals['payments']]
                    parent, to_create = self.create_suggested_line(
                        lines, pending_amount, name=group.rec_name)
                    if not suggested_used:
                        suggested_used = (parent.name if parent
                            else to_create[0]['name'])
                    suggesteds.extend(to_create)

        if payment_groups_clearing['amount'] == abs(pending_amount):
            name = gettext("Payments")
            if len(payment_groups_clearing['groups']) == 1:
                key = payment_groups_clearing['group'].keys()[0]
                if (len(payment_groups_clearing['groups'][key][
                        'payments']) == 1):
                    name = None
            lines = [p.line for v in payment_groups_clearing['groups'].values()
                for p in v['payments']]
            parent, to_create = self.create_suggested_line(lines,
                pending_amount, name=name, payment=True)
            if not suggested_used:
                suggested_used = (parent.name if parent
                    else to_create[0]['name'])
            suggesteds.extend(to_create)
        elif payment_groups_clearing['amount'] != _ZERO:
            for group, vals in payment_groups_clearing['groups'].items():
                if vals['amount'] == abs(pending_amount):
                    lines = [x.line for x in vals['payments']]
                    parent, to_create = self.create_suggested_line(
                        lines, pending_amount, name=group.rec_name,
                        payment=True)
                    if not suggested_used:
                        suggested_used = (parent.name if parent
                            else to_create[0]['name'])
                    suggesteds.extend(to_create)
        return suggesteds, suggested_used

    def _search_suggested_reconciliation_by_move_line(self, exclude=None):
        """
        Search for any move line, not related to invoice or payments that the
        amount it the origin pending_amount
        """
        pool = Pool()
        MoveLine = pool.get('account.move.line')

        suggesteds = []
        suggested_used = None

        # search only for the same ammount and possible party
        pending_amount = self.pending_amount
        if pending_amount == _ZERO:
            return suggesteds, suggested_used

        # Prepapre the base domain
        domain = self._search_move_line_reconciliation_domain()
        domain.extend((
                ('move_origin', 'not like', 'account.invoice,%'),
                ('payments', '=', None),
                ))
        if exclude:
            domain.append(('id', 'not in', [x.id for x in exclude]))
        if pending_amount > 0:
            domain.append(('debit', '=', abs(pending_amount)))
        else:
            domain.append(('credit', '=', abs(pending_amount)))

        for line in MoveLine.search(domain, order=[('maturity_date', 'ASC')]):
            parent, to_create = self.create_suggested_line([line],
                pending_amount)
            if not suggested_used:
                suggested_used = to_create[0]['name']
            suggesteds.extend(to_create)

        return suggesteds, suggested_used

    def _search_suggested_reconciliation_by_text(self):
        """
        Search by the text in Origin information field
        """
        pool = Pool()
        Account = pool.get('account.account')
        Config = pool.get('account.configuration')
        config = Config(1)

        suggesteds = []
        suggested_used = None

        pending_amount = self.pending_amount
        if pending_amount == _ZERO:
            return suggesteds, suggested_used

        remittance_information = self.information.get(
            'remittance_information', None)

        # TODO: If nothing is found search for some special chars:
        #   - TARJ --> move line account 629.0
        #   - COMISION DIVISA NO EURI --> move line account 626.0
        #   - LIQUIDACION GESTION COBRO --> move line account 629.0
        #   - TGSS REGIMEN GENERAL
        # PROVE OF CONCEPT
        texts = {
            'TARJ': '629.0',
            'COMISON': '626.0',
            'GESTION': '629.0',
            'IMPUESTO': '475%',
            'TGSS': '476.0',
            }
        for text, code in texts.items():
            domain = self._search_account_reconciliation_domain()
            if '.' in code:
                code_len = len(code) - 1
                digits = config.default_account_code_digits - code_len
                code = code.replace('.', ''.zfill(digits))
            domain.append(('code', 'like', code))
            accounts = Account.search(domain)
            if not accounts:
                continue
            account = accounts[0]
            # TODO: unaccent!
            if text.upper() in remittance_information.upper():
                name = "%s - %s" % (text.upper(), account.code)
                values = {
                    'name': name,
                    'origin': self,
                    'date': self.date,
                    'account': account,
                    'amount': self.pending_amount,
                    'second_currency': self.second_currency,
                    'state': 'proposed'
                    }
                name_exists = self.check_key_value_exists('name', name,
                    suggesteds)
                if not name_exists:
                    suggesteds.append(values)
                if not suggested_used:
                    suggested_used = name
        return suggesteds, suggested_used

    def _search_account_reconciliation_domain(self):
        return [
            ('company', '=', self.company.id),
            ('currency', '=', self.currency),
            ('type', '!=', None),
            ('closed', '!=', True),
            ]

    def _search_suggested_reconciliation_by_account(self, parties=None):
        """
        Search by the possiblity to have an account wit the aprty name
        If party dose not found by invoice or move line.
        """
        pool = Pool()
        Account = pool.get('account.account')

        suggesteds = []
        suggested_used = None

        pending_amount = self.pending_amount
        if pending_amount == _ZERO or parties is None:
            return suggesteds, suggested_used

        # TODO: For example, have the parties UB and GITHUB and the statement
        # origin have the text "GITHUB", ensure to take the most exact result.
        domain = self._search_account_reconciliation_domain()
        for account in Account.search(domain):
            dom = [
                ('id', 'in', [x.id for x in parties])
                ]
            match_parties = self._match_parties(account.name, domain=dom)
            for party in match_parties:
                name = account.rec_name
                values = {
                    'name': name,
                    'origin': self,
                    'date': self.date,
                    'account': account,
                    'amount': self.pending_amount,
                    'second_currency': self.second_currency,
                    'state': 'proposed'
                    }
                name_exists = self.check_key_value_exists('name', name,
                    suggesteds)
                if not name_exists:
                    suggesteds.append(values)
                if not suggested_used:
                    suggested_used = name
        return suggesteds, suggested_used

    def _search_reconciliation(self):
        pool = Pool()
        SuggestedLine = pool.get('account.statement.origin.suggested.line')
        StatementLine = pool.get('account.statement.line')

        # Before a new search remove all suggested lines, but control if any
        # of them are related to a statement line.
        suggests = SuggestedLine.search([
                ('origin', '=', self),
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

        # Search by possible parties (controling if it is related to an invoice
        # or payments, or are move lines)
        suggesteds, suggested_use, lines, unused_parties = (
            self._search_suggested_reconciliation_by_party())

        # Search by possible invoices unknowing the party
        suggest_lines, suggest_use = (
            self._search_suggested_reconciliation_by_invoice(exclude=lines))
        suggesteds.extend(suggest_lines)
        if not suggested_use:
            suggested_use = suggest_use

        # Search by possible payment group
        suggest_lines, suggest_use, used_groups = (
            self._search_suggested_reconciliation_payment_group())
        suggesteds.extend(suggest_lines)
        if not suggested_use:
            suggested_use = suggest_use

        # Search by possible part of payment group
        # By the moment the payments must have the same date as the Origin
        suggest_lines, suggest_use = (
            self._search_suggested_reconciliation_payment(exclude=used_groups))
        suggesteds.extend(suggest_lines)
        if not suggested_use:
            suggested_use = suggest_use

        # Search by move_line without origin and unknowing party
        suggest_lines, suggest_use = (
            self._search_suggested_reconciliation_by_move_line(exclude=lines))
        suggesteds.extend(suggest_lines)
        if not suggested_use:
            suggested_use = suggest_use

        # Search by any possibiity found in the Origin text information
        suggest_lines, suggest_use = (
            self._search_suggested_reconciliation_by_text())
        suggesteds.extend(suggest_lines)
        if not suggested_use:
            suggested_use = suggest_use

        if not suggesteds:
            # At last search by account name like possible party base on the
            # Origin text information
            suggest_lines, suggest_use = (
                self._search_suggested_reconciliation_by_account(
                    parties=unused_parties))
            suggesteds.extend(suggest_lines)
            if not suggested_use:
                suggested_use = suggest_use

        if suggesteds:
            SuggestedLine.create(suggesteds)
            if suggested_use:
                suggest_lines = SuggestedLine.search([
                    ('origin', '=', self)
                    ('name', '=', suggested_use)
                    ], limit=1)
                if suggest_lines:
                    suggest_line = suggest_lines[0]
                    if suggest_line.second_currency != self.second_currency:
                        self.second_currency = suggest_line.second_currency
                        self.amount_second_currency = (
                            suggest_line.amount_second_currency)
                        self.save()
                    SuggestedLine.use(suggest_lines)

    @classmethod
    @ModelView.button
    def search_suggestions(cls, origins):
        for origin in origins:
            origin._search_reconciliation()

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
    state = fields.Selection([
            ('proposed', "Proposed"),
            ('used', "Used"),
            ], "State", readonly=True, sort=False)

    @classmethod
    def __setup__(cls):
        super().__setup__()
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


class Journal(metaclass=PoolMeta):
    __name__ = 'account.statement.journal'

    aspsp_name = fields.Char("ASPSP Name", readonly=True)
    aspsp_country = fields.Char("ASPSP Country", readonly=True)
    synchronize_journal = fields.Boolean("Synchronize Journal")
    account_statement_origin_sequence = fields.Many2One(
        'ir.sequence', "Account Statement Origin Sequence", required=True,
        domain=[
            ('sequence_type', '=',
                Id('account_statement_enable_banking',
                    'sequence_type_account_statement_origin')),
            ('company', '=', Eval('company')),
            ])

    @classmethod
    def __setup__(cls):
        super(Journal, cls).__setup__()
        cls._buttons.update({
            'synchronize_statement_enable_banking': {},
        })

    def set_number(self, origins):
        '''
        Fill the number field with the statement origin sequence
        '''
        pool = Pool()
        StatementOrigin = pool.get('account.statement.origin')

        for origin in origins:
            if origin.number:
                continue
            origin.number = self.account_statement_origin_sequence.get()
        StatementOrigin.save(origins)

    @classmethod
    @ModelView.button_action('account_statement_enable_banking.'
        'act_enable_banking_synchronize_statement')
    def synchronize_statement_enable_banking(cls, journals):
        pass

    def synchronize_statements_enable_banking(self):
        pool = Pool()
        EBSession = pool.get('enable_banking.session')
        EBConfiguration = pool.get('enable_banking.configuration')
        Statement = pool.get('account.statement')
        StatementOrigin = pool.get('account.statement.origin')
        Date = Pool().get('ir.date')

        ebconfig = EBConfiguration(1)
        # Get the session
        eb_session = EBSession.search([
            ('company', '=', self.company.id),
            ('bank', '=', self.bank_account.bank.id)], limit=1)

        if not eb_session:
            raise AccessError(
                gettext('account_statement_enable_banking.msg_no_session'))

        # Search the account from the journal
        session = eval(eb_session[0].session)
        bank_numbers = [x.number_compact for x in self.bank_account.numbers]
        account_id = None
        for account in session['accounts']:
            if account['account_id']['iban'] in bank_numbers:
                account_id = account['uid']
                break
        if not account_id:
            raise AccessError(
                gettext('account_statement_enable_banking.'
                    'msg_account_not_found',
                    account=bank_numbers,
                    bank=eb_session.bank.party.name))

        # Prepare request
        base_headers = get_base_header()
        query = {
            "date_from": (datetime.now(timezone.utc) - timedelta(
                    days=ebconfig.offset)).date().isoformat()}

        # We need to create an statement, as is a required field for the origin
        statement = Statement()
        statement.company = self.company
        statement.name = self.name
        statement.date = Date.today()
        statement.journal = self
        statement.on_change_journal()
        statement.end_balance = Decimal(0)
        if not statement.start_balance:
            statement.start_balance = Decimal(0)
        statement.save()

        # Get the data, as we have a limit of transactions every query, we need
        # to do a while loop to get all the transactions
        continuation_key = None
        to_save = []
        total_amount = 0
        while True:
            if continuation_key:
                query["continuation_key"] = continuation_key

            r = requests.get(
                f"{config.get('enable_banking', 'api_origin')}"
                f"/accounts/{account_id}/transactions",
                params=query, headers=base_headers)
            if r.status_code == 200:
                response = r.json()
                continuation_key = response.get('continuation_key')
                for transaction in response['transactions']:
                    if (transaction['transaction_amount']['currency'] !=
                            self.currency.code):
                        raise AccessError(gettext(
                                'account_statement_enable_banking.'
                                'msg_currency_not_match'))
                    found_statement_origin = StatementOrigin.search([
                        ('entry_reference', '=',
                            transaction['entry_reference']),
                        ])
                    if found_statement_origin:
                        continue
                    statement_origin = StatementOrigin()
                    statement_origin.number = None
                    statement_origin.state = 'registered'
                    statement_origin.statement = statement
                    statement_origin.company = self.company
                    statement_origin.currency = self.currency
                    statement_origin.amount = (
                            transaction['transaction_amount']['amount'])
                    if (transaction['credit_debit_indicator'] and
                            transaction['credit_debit_indicator'] == 'DBIT'):
                        statement_origin.amount = -statement_origin.amount
                    total_amount += statement_origin.amount
                    statement_origin.entry_reference = transaction[
                        'entry_reference']
                    statement_origin.date = datetime.strptime(
                        transaction[ebconfig.date_field], '%Y-%m-%d')
                    information_dict = {}
                    for key, value in transaction.items():
                        if value is None:
                            continue
                        information_dict[key] = str(value)
                    statement_origin.information = information_dict
                    to_save.append(statement_origin)
                if not continuation_key:
                    statement.end_balance = (
                        statement.start_balance + total_amount)
                    statement.save()
                    break
            else:
                raise AccessError(
                    gettext('account_statement_enable_banking.'
                        'msg_error_get_statements',
                        error=str(r.status_code),
                        error_message=str(r.text)))

        to_save.sort(key=lambda x: x.date)
        # The set number function save the origins
        self.set_number(to_save)

        # Get the suggested lines for each origin created
        for origin in statement.origins:
            origin._search_reconciliation()

    @classmethod
    def synchronize_enable_banking_journals(cls):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        for journal in Journal.search([('synchronize_journal', '=', True)]):
            journal.synchronize_statements_enable_banking()


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
