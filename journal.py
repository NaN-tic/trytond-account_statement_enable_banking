# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import requests
from decimal import Decimal
from datetime import datetime, timedelta

from trytond.pool import Pool, PoolMeta
from trytond.model import ModelView, fields
from trytond.pyson import Eval, Id
from trytond.config import config
from trytond.i18n import gettext
from trytond.transaction import Transaction
from trytond.model.exceptions import AccessError
from .common import get_base_header


class Journal(metaclass=PoolMeta):
    __name__ = 'account.statement.journal'

    similarity_threshold = fields.Integer('Similarity Threshold',
        required=True,
        domain=[
            ('similarity_threshold', '>', 0),
            ('similarity_threshold', '<=', 10),
            ],
        help='The thershold used for similarity function in origin lines '
        'search')
    acceptable_similarity = fields.Integer(
        'Acceptable Similarity', required=True,
        domain=[
            ('acceptable_similarity', '>', 0),
            ('acceptable_similarity', '<=', 10),
            ],
        help='The minimum similarity allowed to set the statement line '
        'direclty from suggested lines.')
    aspsp_name = fields.Char("ASPSP Name", readonly=True)
    aspsp_country = fields.Char("ASPSP Country", readonly=True)
    synchronize_journal = fields.Boolean("Synchronize Journal",
        help="Check if want to synchronize automatically. When is "
        "automatically the offset is not used and tak the las "
        "statement synched date.")
    account_statement_origin_sequence = fields.Many2One(
        'ir.sequence', "Account Statement Origin Sequence", required=True,
        domain=[
            ('sequence_type', '=',
                Id('account_statement_enable_banking',
                    'sequence_type_account_statement_origin')),
            ['OR',
                ('company', '=', Eval('company')),
                ('company', '=', None),
            ]])
    enable_banking_session = fields.Many2One('enable_banking.session',
        'Enable Banking Session', readonly=True)
    enable_banking_session_valid_days = fields.TimeDelta('Enable Banking Session Valid Days',
        help="Only allowed maximum 180 days.")
    one_move_per_origin = fields.Boolean("One Move per Origin",
        help="Check if want to create only one move per origin when post it "
        "even it has more than one line. Else it create one move for eaach "
        "line.")
    min_amount_tolerance = fields.Numeric('Min Amount tolerance',
        domain=[
            ('min_amount_tolerance', '>=', 0),
            ('min_amount_tolerance', '<=', 99999999),
            ('min_amount_tolerance', '<=', Eval('max_amount_tolerance')),
            ],
        help="In some cases, it is possible to have amounts that vary in X. "
        "This field if set is the minimum of the allowed tolerance. That is, "
        "if value is set when searching for similarities it will look for "
        "equal amounts or with -X, value that has been set here.")
    max_amount_tolerance = fields.Numeric('Max Amount tolerance',
        domain=[
            ('max_amount_tolerance', '>=', 0),
            ('max_amount_tolerance', '<=', 99999999),
            ('max_amount_tolerance', '>=', Eval('min_amount_tolerance')),
            ],
        help="In some cases, it is possible to have amounts that vary in X. "
        "This field if set is the maximum of the allowed tolerance. That is, "
        "if value is set when searching for similarities it will look for "
        "equal amounts or with +X, value that has been set here.")

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({
            'synchronize_statement_enable_banking': {},
        })

    @staticmethod
    def default_validation():
        return 'balance'

    @staticmethod
    def default_similarity_threshold():
        return 5

    @staticmethod
    def default_acceptable_similarity():
        return 8

    @staticmethod
    def default_one_move_per_origin():
        return False

    @staticmethod
    def default_min_amount_tolerance():
        return 0

    @staticmethod
    def default_max_amount_tolerance():
        return 0

    @staticmethod
    def default_enable_banking_session_valid_days():
        return timedelta(days=30) # 30 days

    @classmethod
    def validate(cls, journals):
        super().validate(journals)
        for journal in journals:
            journal.check_enable_banking_session_valid_days()

    def check_enable_banking_session_valid_days(self):
        if (self.enable_banking_session_valid_days < timedelta(days=1)
                or self.enable_banking_session_valid_days > timedelta(
                    days=180)):
            raise AccessError(
                gettext('account_statement_enable_banking.'
                    'msg_valid_days_out_of_range'))

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
        'act_enable_banking_synchronize_statement')
    def synchronize_statement_enable_banking(cls, journals):
        pass

    def synchronize_statements_enable_banking(self):
        pool = Pool()
        EBConfiguration = pool.get('enable_banking.configuration')
        Statement = pool.get('account.statement')
        StatementOrigin = pool.get('account.statement.origin')
        Date = Pool().get('ir.date')

        ebconfig = EBConfiguration(1)

        if (not self.enable_banking_session
                or not self.enable_banking_session.session
                or self.enable_banking_session.valid_until < datetime.now()):
            return

        # Search the account from the journal
        session = eval(self.enable_banking_session.session)
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
        date = None
        base_headers = get_base_header()
        statements = Statement.search([
                ('journal', '=', self.id),
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
            date = last_statement.end_date
        if not date:
            date = datetime.now()
        date_from = (date - timedelta(days=ebconfig.offset or 2)).date()
        query = {
            "date_from": date_from.isoformat()
            }

        # We need to create an statement, as is a required field for the origin
        statement = Statement()
        statement.company = self.company
        statement.name = self.name
        statement.date = Date.today()
        statement.journal = self
        statement.on_change_journal()
        statement.end_balance = Decimal(0)
        if (not hasattr(statement, 'start_balance')
                or statement.start_balance is None):
            statement.start_balance = Decimal(0)
        statement.start_date = datetime.combine(date_from, datetime.min.time())
        statement.end_date = datetime.now()
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
                    found_statement_origin = StatementOrigin.search([
                        ('entry_reference', '=', entry_reference),
                        ])
                    if found_statement_origin:
                        continue
                    # TODO:
                    # Ensure transaction_amount.currency == origin.currency
                    statement_origin = StatementOrigin()
                    statement_origin.entry_reference = entry_reference
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
                    balance_after_transaction = transaction.get(
                        'balance_after_transaction', {})
                    if balance_after_transaction:
                        statement_origin.balance = (
                            balance_after_transaction.get('amount', None))
                    total_amount += statement_origin.amount
                    statement_origin.date = datetime.strptime(
                        transaction[ebconfig.date_field], '%Y-%m-%d')
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
                        error_code=str(r.status_code),
                        error_message=str(r.text)))

        if to_save:
            StatementOrigin.save(to_save)
            to_save.sort(reverse=True)
            to_save.sort(key=lambda x: x.date)
            self.set_number(to_save)

            # Get the suggested lines for each origin created
            # Use __queue__ to ensure the Bank lines download and origin
            # creation are done and saved before start to create there
            # suggestions.
            StatementOrigin.__queue__._search_reconciliation(statement.origins)
        else:
            with Transaction().set_context(_skip_warnings=True):
                Statement.validate_statement([statement])
                Statement.post([statement])

    @classmethod
    def synchronize_enable_banking_journals(cls):
        pool = Pool()
        Journal = pool.get('account.statement.journal')

        company_id = Transaction().context.get('company')
        if not company_id:
            return
        for journal in Journal.search([
                ('synchronize_journal', '=', True),
                ('company.id', '=', company_id),
                ]):
            journal.synchronize_statements_enable_banking()


class Cron(metaclass=PoolMeta):
    __name__ = 'ir.cron'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.method.selection.extend([
            ('account.statement.journal|synchronize_enable_banking_journals',
                "Synchronize Enable Banking Journals"),
            ])
