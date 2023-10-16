# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import requests
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from secrets import token_hex
from itertools import groupby

from trytond.model import Workflow, ModelView, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval
from trytond.rpc import RPC
from trytond.wizard import (
    Button, StateAction, StateTransition, StateView, Wizard)
from trytond.transaction import Transaction
from trytond.config import config
from .common import get_base_header
from trytond.i18n import gettext
from trytond.exceptions import UserError
from trytond.modules.account_statement.exceptions import (StatementValidateError,
    StatementValidateWarning)


class Statement(metaclass=PoolMeta):
    __name__ = 'account.statement'

    @classmethod
    def cancel(cls, statements):
        pool = Pool()
        Origin = pool.get('account.statement.origin')

        origins = [s.origin for s in statements]
        Origin.cancel(origins)

        super().cancel(statements)

    @classmethod
    def delete(cls, statements):
        raise UserError(gettext('account_statement_enable_banking.'
                'msg_no_allow_delete_only_cancel'))


class Line(metaclass=PoolMeta):
    __name__ = 'account.statement.line'

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
    def delete(cls, lines):
        '''As is needed save an history fo all movements, do not remove the
        possible move related. Create the cancelation move and leave they
        related to the statement and the origin, to have an hstory.
        '''
        pool = Pool()
        Move = pool.get('account.move')
        MoveLine = pool.get('account.move.line')
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
                #line_moves = list(set([l.move for l in mlines]))
                #Move.write(line_moves, {'state': 'draft'})
                MoveLine.save(mlines)
                #Move.post(line_moves)
        cls.cancel_move(lines)
        super().delete(lines)


class Origin(Workflow, metaclass=PoolMeta):
    __name__ = 'account.statement.origin'

    entry_reference = fields.Char("Entry Reference", readonly=True)
    state = fields.Selection([
            ('registered', "Registered"),
            ('cancelled', "Cancelled"),
            ('posted', "Posted"),
            ], "State", readonly=True, sort=False)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order.insert(0, ('date', 'ASC'))
        cls._order.insert(1, ('statement', 'ASC'))
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
                })
        cls.__rpc__.update({
                'post': RPC(
                    readonly=False, instantiate=0, fresh_session=True),
                })

    @fields.depends('statement', 'lines')
    def on_change_lines(self):
        if not self.statement.journal or not self.statement.company:
            return
        if self.statement.journal.currency != self.statement.company.currency:
            return

        invoices = set()
        payments = set()
        for line in self.lines:
            if (line.invoice
                    and line.invoice.currency == self.company.currency):
                invoices.add(line.invoice)
            if (line.payment
                    and line.payment.currency == self.company.currency):
                payments.add(line.payment)
        invoice_id2amount_to_pay = {}
        for invoice in invoices:
            if invoice.type == 'out':
                sign = -1
            else:
                sign = 1
            invoice_id2amount_to_pay[invoice.id] = sign * invoice.amount_to_pay

        payment_id2amount = (dict((x.id, x.amount) for x in payments)
            if payments else {})

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
                moves.append((move, lines))

        Move.save([m for m, _ in moves])

        to_write = []
        for move, lines in moves:
            to_write.extend((lines, {'move': move.id}))
        if to_write:
            StatementLine.write(*to_write)

        move_lines = []
        for move, lines in moves:
            amount = 0
            amount_second_currency = 0
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
        # Warning of statement state in Move.post is not applied when try to
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
        statements = []
        for origin in origins:
            if origin.statement.state != 'draft':
                statements.append(origin.statements)
        if statements:
            Statement.draft(statements)

    @classmethod
    def delete(cls, origins):
        raise UserError(gettext('account_statement_enable_banking.'
                'msg_no_allow_delete_only_cancel'))


class Journal(metaclass=PoolMeta):
    __name__ = 'account.statement.journal'

    aspsp_name = fields.Char("ASPSP Name", readonly=True)
    aspsp_country = fields.Char("ASPSP Country", readonly=True)
    synchronize_journal = fields.Boolean("Synchronize Journal")

    @classmethod
    def __setup__(cls):
        super(Journal, cls).__setup__()
        cls._buttons.update({
            'synchronize_statement_enable_banking': {},
        })

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
            raise UserError(
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
            raise UserError(
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
        statement.journal = self
        statement.date = Date.today()
        statement.end_balance = Decimal(0)
        statement.start_balance = Decimal(0)
        statement.save()

        # Get the data, as we have a limit of transactions every query, we need
        # to do a while loop to get all the transactions
        continuation_key = None
        to_save = []
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
                        raise UserError(gettext(
                                'account_statement_enable_banking.'
                                'msg_currency_not_match'))
                    found_statement_origin = StatementOrigin.search([
                        ('entry_reference', '=',
                            transaction['entry_reference']),
                        ])
                    if found_statement_origin:
                        continue
                    statement_origin = StatementOrigin()
                    statement_origin.state = 'registered'
                    statement_origin.statement = statement
                    statement_origin.company = self.company
                    statement_origin.currency = self.currency
                    statement_origin.amount = (
                            transaction['transaction_amount']['amount'])
                    if (transaction['credit_debit_indicator'] and
                            transaction['credit_debit_indicator'] == 'DBIT'):
                        statement_origin.amount = -statement_origin.amount
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
                    statement.end_balance = transaction.get(
                        'eb_balance_after_transaction', 0)
                    statement.save()
                    break
            else:
                raise UserError(
                    gettext('account_statement_enable_banking.'
                        'msg_error_get_statements',
                        error=str(r.status_code),
                        error_message=str(r.text)))
        StatementOrigin.save(to_save)

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
            raise UserError(gettext(
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
            raise UserError(gettext('account_statement_enable_banking.'
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
            raise UserError(
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
            raise UserError(
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
