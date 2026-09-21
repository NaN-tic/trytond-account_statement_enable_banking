# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from datetime import datetime, UTC, timedelta
from secrets import token_hex

import requests

import trytond.config as config
from trytond.i18n import gettext
from trytond.model import ModelView, fields
from trytond.model.exceptions import AccessError
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval, PYSONEncoder
from trytond.transaction import Transaction
from trytond.wizard import (
    Button, StateAction, StateTransition, StateView, Wizard)

from .common import get_base_header, load_session_json, URL, REDIRECT_URL

PRODUCTION = config.get('database', 'production', default=False)


class Origin(metaclass=PoolMeta):
    __name__ = 'account.statement.origin'

    @fields.depends('statement', '_parent_statement.journal')
    def on_change_with_synchronized(self, name=None):
        if self.statement and self.statement.journal.enable_banking_session:
            return True
        return super().on_change_with_synchronized(name)


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
            Button('Use Existing Session', 'use_session', 'tryton-refresh',
                default=True),
        ])
    create_session = StateAction(
        'account_statement_enable_banking.url_session')
    use_session = StateTransition()

    def transition_check_session_before_start(self):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        base_headers = get_base_header()

        active_id = Transaction().context.get('active_id', None)
        journal = Journal(active_id) if active_id else None
        if not journal or not journal.bank_account:
            raise AccessError(gettext(
                    'account_statement_enable_banking.msg_no_bank_account'))

        if journal.enable_banking_session:
            # Reauthorize missing or expired sessions.
            eb_session = journal.enable_banking_session
            if eb_session.session and not eb_session.session_expired:
                session = load_session_json(eb_session.session)
                r = requests.get(
                    f"{URL}/sessions/{session['session_id']}",
                    headers=base_headers)
                if r.status_code == 200:
                    session = r.json()
                    if session['status'] == 'AUTHORIZED':
                        return 'end'
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

        eb_sessions = EBSession.search([
            ('bank', '=', journal.bank_account.bank),
            ('allowed_bank_accounts', '=', journal.bank_account),
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
            ('allowed_bank_accounts', '=', journal.bank_account),
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
            if (not journal.bank_account or not journal.bank_account.bank or
                    not journal.bank_account.bank.party):
                raise AccessError(gettext('account_statement_enable_banking.'
                        'msg_no_bank_account'))
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

            # Resolve the ASPSP using the bank account.
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
                    gettext('account_statement_enable_banking.'
                        'msg_aspsp_not_found',
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
    origin = StateAction('account_statement_common.act_statement_origin_form')
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
                ('synchronize_journal', '=', True),
                ('statement_provider', '=', 'enable_banking'),
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
