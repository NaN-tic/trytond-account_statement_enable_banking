# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import requests
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from secrets import token_hex

from trytond.model import ModelView, fields
from trytond.pool import Pool, PoolMeta
from trytond.wizard import (
    Button, StateAction, StateTransition, StateView, Wizard)
from trytond.transaction import Transaction
from trytond.config import config
from .common import get_base_header
from trytond.i18n import gettext
from trytond.exceptions import UserError


class Origin(metaclass=PoolMeta):
    __name__ = 'account.statement.origin'

    entry_reference = fields.Char("Entry Reference", readonly=True)

    @classmethod
    def __setup__(cls):
        super(Origin, cls).__setup__()
        cls._buttons.update({
            'reconcile': {}
        })

    #TODO: check the button, to see if it works correctly
    @classmethod
    @ModelView.button
    def reconcile(cls, origins):
        pool = Pool()
        Line = pool.get('account.statement.line')

        to_reconcilie = []
        for origin in origins:
            if origin.lines:
                for line in origin.lines:
                    to_reconcilie.append(line)
        Line.reconcile(to_reconcilie)


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
    @ModelView.button_action(
        'account_statement_enable_banking.act_enable_banking_synchronize_statement')
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
                gettext('account_statement_enable_banking.msg_account_not_found',
                    account=bank_numbers,
                    bank=eb_session.bank.party.name))

        # Prepare request
        base_headers = get_base_header()
        query = {
            "date_from": (datetime.now(timezone.utc) - timedelta(
                days=ebconfig.offset)).date().isoformat(),}

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

            r = requests.get(f"{config.get('enable_banking', 'api_origin')}/accounts/{account_id}/transactions",
                params=query, headers=base_headers,)
            if r.status_code == 200:
                response = r.json()
                continuation_key = response.get('continuation_key')
                for transaction in response['transactions']:
                    if transaction['transaction_amount']['currency'] != self.currency.code:
                        raise UserError(gettext(
                            'account_statement_enable_banking.msg_currency_not_match'))
                    found_statement_origin = StatementOrigin.search([
                        ('entry_reference', '=', transaction['entry_reference']),
                    ])
                    if found_statement_origin:
                        continue
                    statement_origin = StatementOrigin()
                    statement_origin.statement = statement
                    statement_origin.company = self.company
                    statement_origin.currency = self.currency
                    statement_origin.amount = (
                            transaction['transaction_amount']['amount'])
                    if (transaction['credit_debit_indicator'] and
                            transaction['credit_debit_indicator'] == 'CRDT'):
                        statement_origin.amount = -statement_origin.amount
                    statement_origin.entry_reference = transaction['entry_reference']
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
                    break
            else:
                raise UserError(
                    gettext('account_statement_enable_banking.msg_error_get_statements',
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
        'account_statement_enable_banking.enable_banking_synchronize_statement_start_form',
        [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('OK', 'check_session', 'tryton-ok', default=True),
        ])
    check_session = StateTransition()
    create_session = StateAction('account_statement_enable_banking.url_session')
    sync_statements = StateTransition()

    def transition_check_session(self):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        EBSession = pool.get('enable_banking.session')

        journal = Journal(Transaction().context['active_id'])
        if not journal.bank_account:
            raise UserError(gettext('account_statement_enable_banking.msg_no_bank_account'))

        bank_name = journal.bank_account.bank.party.name.lower()
        bic = (journal.bank_account.bank.bic or '').lower()
        if journal.bank_account.bank.party.addresses:
            country = journal.bank_account.bank.party.addresses[0].country.code
        else:
            raise UserError(gettext('account_statement_enable_banking.msg_no_country'))

        # We fill the aspsp name and country using the bank account
        base_headers = get_base_header()
        r = requests.get(f"{config.get('enable_banking', 'api_origin')}/aspsps", headers=base_headers)
        aspsp_found = False
        for aspsp in r.json()["aspsps"]:
            if aspsp["country"] != country:
                continue
            if (aspsp["name"].lower() == bank_name
                    or aspsp["bic"].lower() == bic):
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

        eb_session = EBSession.search([
            ('company', '=', journal.company.id),
            ('bank', '=', journal.bank_account.bank.id)], limit=1)
        if eb_session:
            # We need to check the date and if we have the field session, if not
            # the session was not created correctly and need to be deleted
            session = eval(eb_session[0].session)
            r = requests.get(f"{config.get('enable_banking', 'api_origin')}/sessions/{session['session_id']}",
                headers=base_headers)
            if r.status_code == 200:
                session = r.json()
                if (session['status'] == 'AUTHORIZED' and
                        datetime.now() < eb_session[0].valid_until and
                        eb_session[0].session):
                    return 'sync_statements'
            else:
                EBSession.delete([eb_session])
        return 'create_session'

    def do_create_session(self, action):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        EBSession = pool.get('enable_banking.session')
        journal = Journal(Transaction().context['active_id'])
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
            action['url'] =r.json()['url']
        else:
            raise UserError(
                gettext('account_statement_enable_banking.msg_error_create_session',
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
