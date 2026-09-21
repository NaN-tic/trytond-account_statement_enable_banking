# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import time
import requests
from datetime import datetime, timedelta
from decimal import Decimal
import trytond.config as config
from trytond.pool import Pool, PoolMeta
from trytond.model import ModelView, fields
from trytond.pyson import Bool, Eval, If
from trytond.i18n import gettext
from trytond.transaction import Transaction
from trytond.model.exceptions import AccessError
from .common import get_base_header, load_session_json, URL

QUEUE_NAME = config.get('enable_banking', 'queue_name', default='default')

class Journal(metaclass=PoolMeta):
    __name__ = 'account.statement.journal'

    aspsp_name = fields.Char("ASPSP Name", readonly=True)
    aspsp_country = fields.Char("ASPSP Country", readonly=True)
    synchronize_journal = fields.Boolean("Synchronize Journal",
        help="Check if want to synchronize automatically. When is "
        "automatically the offset is not used and tak the las "
        "statement synched date.")
    enable_banking_session = fields.Many2One('enable_banking.session',
        'Enable Banking Session',
        domain=[
                If(Eval('statement_provider') == 'enable_banking',
                    ('allowed_bank_accounts', '=', Eval('bank_account')), ()),
                ])
    enable_banking_session_allowed_bank_accounts = fields.Function(
        fields.Many2Many('bank.account', None, None, 'Allowed Bank Accounts',
            context={
                'company': Eval('company', -1),
                }, depends={'company'}, readonly=True),
        'on_change_with_enable_banking_session_allowed_bank_accounts')
    offset_days_to = fields.Integer('Offset Days To',
        domain=[
            ('offset_days_to', '<=', 20),
            ('offset_days_to', '>=', 0)
            ],
        help='Default offset days in the Bank transaction search. '
        'Allow to not download "to" today, could be set "to" some days before.'
        ' This field could be from 0 to 20. 0 meaning today, other value will '
        'be substracted from today.')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.statement_provider.selection.append(
            ('enable_banking', 'Enable Banking'))
        cls.bank_account.domain.append(
            If((Eval('statement_provider') == 'enable_banking')
                    & Bool(Eval('enable_banking_session_allowed_bank_accounts', [])),
                ('id', 'in',
                    Eval('enable_banking_session_allowed_bank_accounts', [])),
                (),
                ))
        cls._buttons.update({
                'retrieve_enable_banking_session': {},
                'synchronize_statement_enable_banking': {},
                })

    @staticmethod
    def default_statement_provider():
        return 'enable_banking'

    @fields.depends('enable_banking_session')
    def on_change_with_enable_banking_session_allowed_bank_accounts(self,
            name=None):
        if self.enable_banking_session:
            return self.enable_banking_session.allowed_bank_accounts

    @fields.depends('enable_banking_session', 'aspsp_name', 'aspsp_country')
    def on_change_enable_banking_session(self):
        if self.enable_banking_session:
            if not self.aspsp_name:
                self.aspsp_name = self.enable_banking_session.aspsp_name
            if not self.aspsp_country:
                self.aspsp_country = self.enable_banking_session.aspsp_country

    @staticmethod
    def default_offset_days_to():
        return 0

    def _keys_not_needed(self):
        # Main keys
        keys = [
            'entry_reference',
            'balance_after_transaction',
            'transaction_amount',
            'credit_debit_indicator',
            'status',
            ]
        # Sub keys
        keys += [
            'organisation_id',
            'private_id',
            'clearing_system_member_id',
            ]
        return keys

    @classmethod
    @ModelView.button_action('account_statement_enable_banking.'
        'act_enable_banking_retrieve_session')
    def retrieve_enable_banking_session(cls, journals):
        pass

    @classmethod
    @ModelView.button
    def synchronize_statement_enable_banking(cls, journals):
        for journal in journals:
            journal._synchronize_statements_enable_banking()

    def _synchronize_statements_enable_banking(self):
        if self.statement_provider != 'enable_banking':
            return
        pool = Pool()
        EBConfiguration = pool.get('enable_banking.configuration')
        Statement = pool.get('account.statement')
        Date = pool.get('ir.date')

        ebconfig = EBConfiguration(1)
        today = Date.today()

        if not self.enable_banking_session:
            raise AccessError(
                gettext('account_statement_enable_banking.msg_no_session'))

        if (
            not self.enable_banking_session.encrypted_session
            or (
                self.enable_banking_session.valid_until
                and self.enable_banking_session.valid_until.date() < today)):
            return

        # Search the account from the journal
        session = load_session_json(self.enable_banking_session.session)
        if not self.bank_account:
            raise AccessError(gettext(
                    'account_statement_enable_banking.msg_no_bank_account'))
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
                    bank=self.enable_banking_session.bank.party.name))

        # Prepare request
        date_from = today
        base_headers = get_base_header()
        statements = Statement.search([
                ('journal', '=', self.id),
                ('end_date', '!=', None),
                ], order=[
                    ('end_date', 'DESC'),
                    ('id', 'DESC'),
                    ], limit=1)
        if statements:
            last_statement, = statements
            # When synch automatically, by crons, take the last Statement
            # of the same journal and get it's end_date to sych from there,
            # to ensure not lost any thing in the same minute add a delta
            # of -1 hour.
            date_from = last_statement.end_date.date()

        date_from = (date_from - timedelta(
            days=ebconfig.offset is not None and ebconfig.offset or 2))
        # date_from parameter cannot be in the future
        if date_from > today:
            date_from = today
        date_to = ((today - timedelta(days=self.offset_days_to or 0)))
        if date_from > date_to:
            return

        query = {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            }

        statement = self.create_import_statement(date_from, date_to)

        # Get the data, as we have a limit of transactions every query, we need
        # to do a while loop to get all the transactions
        continuation_key = None
        to_save = []
        last_transaction_date = None
        continuation_key_error = False
        while True:
            if continuation_key:
                query["continuation_key"] = continuation_key

            for retry in range(3):
                try:
                    r = requests.get(
                        f"{URL}/accounts/{account_id}/transactions",
                        params=query, headers=base_headers)
                    break
                except requests.RequestException as e:
                    if retry == 2:
                        raise AccessError(
                            gettext('account_statement_enable_banking.'
                                'msg_error_get_statements',
                                error_code='N/A',
                                error_message=str(e)))
                    time.sleep(2 ** retry)

            if r.status_code == 200:
                response = r.json()
                continuation_key = response.get('continuation_key')
                last_transaction_date = None
                transactions = []
                for transaction in response['transactions']:
                    entry_reference = transaction.get('entry_reference', None)
                    # The entry_reference is set to None if not exist in
                    # transaction result, but could exist and be and empty
                    # string so control the "not", instead of "is None".
                    if not entry_reference:
                        continue
                    if (transaction['transaction_amount']['currency'] !=
                            self.currency.code):
                        raise AccessError(gettext(
                                'account_statement_enable_banking.'
                                'msg_currency_not_match'))
                    amount = Decimal(str(
                        transaction['transaction_amount']['amount']))
                    if transaction.get('credit_debit_indicator') == 'DBIT':
                        amount = -amount
                    balance_after_transaction = transaction.get(
                        'balance_after_transaction', {})
                    balance = None
                    if balance_after_transaction:
                        balance = Decimal(str(
                            balance_after_transaction['amount']))
                    transaction_date = datetime.strptime(
                        transaction[ebconfig.date_field], '%Y-%m-%d').date()
                    information_dict = {}
                    for key, value in transaction.items():
                        if value is None or key in self._keys_not_needed():
                            continue
                        if isinstance(value, str):
                            information_dict[key] = value
                        elif isinstance(value, bytes):
                            information_dict[key] = str(value)
                        elif isinstance(value, dict):
                            for k, v in value.items():
                                if v is None or k in self._keys_not_needed():
                                    continue
                                tag = "%s_%s" % (key, k)
                                if isinstance(v, str):
                                    information_dict[tag] = v
                                elif isinstance(v, bytes):
                                    information_dict[tag] = str(v)
                        elif isinstance(value, list):
                            information_dict[key] = ", ".join(value)
                    transactions.append({
                        'entry_reference': entry_reference,
                        'date': transaction_date,
                        'amount': amount,
                        'balance': balance,
                        'description': ', '.join(transaction.get(
                            'remittance_information', [])),
                        'information': information_dict,
                        })
                origins = self.import_statement_transactions(
                    statement, transactions)
                to_save.extend(origins)
                if origins:
                    last_transaction_date = origins[-1].date
                if not continuation_key:
                    break
            if ((r.status_code == 400 or continuation_key_error)
                    and continuation_key):
                continuation_key_error = True
                continuation_key = None
                if (last_transaction_date
                        and last_transaction_date != query["date_from"]):
                    # TODO: Remove when some Spanish Bnaks solve the recursive
                    # calls problem. (eg: Bankinter)
                    # If the problem with the continuation_key is not solved
                    # and in one day you have more than 30 transactions, this
                    # patch will not solve the problem correctly.
                    query["date_from"] = last_transaction_date.isoformat()
                else:
                    date_obj = datetime.strptime(query["date_from"],
                        "%Y-%m-%d")
                    next_day = date_obj + timedelta(days=1)
                    query["date_from"] = next_day.strftime("%Y-%m-%d")
                if query["date_from"] > query["date_to"]:
                    break
            elif r.status_code != 200:
                raise AccessError(
                    gettext('account_statement_enable_banking.'
                        'msg_error_get_statements',
                        error_code=str(r.status_code),
                        error_message=str(r.text)))

        self.finish_import_statement(statement, to_save)

    @classmethod
    def synchronize_enable_banking_journals(cls):
        pool = Pool()
        Journal = pool.get('account.statement.journal')

        company_id = Transaction().context.get('company')
        if not company_id:
            return

        with Transaction().set_context(queue_name=QUEUE_NAME):
            for journal in Journal.search([
                    ('synchronize_journal', '=', True),
                    ('statement_provider', '=', 'enable_banking'),
                    ('company.id', '=', company_id),
                    ]):
                cls.__queue__._synchronize_statements_enable_banking(journal)

    @classmethod
    def set_ebsession(cls, eb_session):
        pool = Pool()
        EBSession = pool.get('enable_banking.session')

        if not eb_session.encrypted_session:
            return

        journals = cls.search([
                ('bank_account', 'in', eb_session.allowed_bank_accounts),
                ])
        old_session_ids = {
            journal.enable_banking_session.id
            for journal in journals
            if (journal.enable_banking_session
                and journal.enable_banking_session != eb_session)
            }
        to_save = []
        for journal in journals:
            journal.enable_banking_session = eb_session
            journal.on_change_enable_banking_session()
            to_save.append(journal)
        if to_save:
            cls.save(to_save)

        if not old_session_ids:
            return

        used_session_ids = {
            journal.enable_banking_session.id
            for journal in cls.search([
                    ('enable_banking_session', 'in', list(old_session_ids)),
                    ])
            if journal.enable_banking_session
            }
        obsolete_session_ids = list(old_session_ids - used_session_ids)
        if obsolete_session_ids:
            EBSession.delete(EBSession.browse(obsolete_session_ids))


class Cron(metaclass=PoolMeta):
    __name__ = 'ir.cron'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.method.selection.extend([
            ('account.statement.journal|synchronize_enable_banking_journals',
                "Synchronize Enable Banking Journals"),
            ])
