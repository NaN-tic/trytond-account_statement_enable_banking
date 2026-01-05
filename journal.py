# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import json
import requests
from collections import defaultdict
from decimal import Decimal
from datetime import datetime, timedelta
from trytond.cache import Cache
from trytond.config import config
from trytond.pool import Pool, PoolMeta
from trytond.model import ModelSQL, ModelView, fields, Unique
from trytond.pyson import Eval, Id, If
from trytond.i18n import gettext
from trytond.transaction import Transaction
from trytond.model.exceptions import AccessError
from trytond.exceptions import UserError
from .common import get_base_header, URL

QUEUE_NAME = config.get('enable_banking', 'queue_name', default='default')

DEFAULT_WEIGHTS = {
    'based-on-match': 10,
    'combination-escape-threshold': 130,
    'date-match': 60,
    'escape-threshold': 150,
    'move-line-max-count': 20,
    'number-match': 20,
    'origin-delta-days': 365,
    'origin-similarity': 80,
    'origin-similarity-threshold': 50,
    'party-match': 20,
    'party-uniformity': 10,
    'max-suggestion-count': 10,
    # 100.000 combinations takes between 0.02 and 0.1 seconds in a laptop
    # 1.000.000 combinations takes between 0.2 and 1 seconds in a laptop
    # Note that it will be computed up to 20 times, so multiply by 20 to get
    # the total time
    'target-combinations': 100_000,
    'type-combination-party': 105,
    'type-combination-all': 100,
    'type-payment-group': 130,
    'type-payment': 120,
    'type-origin': 100,
    'type-balance': 90,
    'type-balance-invoice': 90,
    'type-sale': 102,
    }


class JournalWeight(ModelSQL, ModelView):
    'Journal Weight'
    __name__ = 'account.statement.journal.weight'

    journal = fields.Many2One('account.statement.journal', 'Journal',
        required=True, ondelete='CASCADE')
    type = fields.Selection([
            ('based-on-match', 'Based On Match'),
            ('combination-escape-threshold', 'Combination Escape Threshold'),
            ('date-match', 'Date Match'),
            ('escape-threshold', 'Escape Threshold'),
            ('max-suggestion-count', 'Max Suggestion Count'),
            ('move-line-max-count', 'Move Line Max Count'),
            ('number-match', 'Number Match'),
            ('origin-delta-days', 'Origin Delta Days'),
            ('origin-similarity', 'Origin Similarity'),
            ('origin-similarity-threshold', 'Origin Similarity Threshold'),
            ('party-match', 'Party Match'),
            ('party-uniformity', 'Party Uniformity'),
            ('target-combinations', 'Target Combinations'),
            ('type-combination-party', 'Type Combination Party'),
            ('type-combination-all', 'Type Combination All'),
            ('type-payment-group', 'Type Payment Group'),
            ('type-payment', 'Type Payment'),
            ('type-origin', 'Type Origin'),
            ('type-balance', 'Type Balance'),
            ('type-balance-invoice', 'Type Balance Invoice'),
            ('type-sale', 'Type Sale'),
            ], 'Type', required=True)
    weight = fields.Integer('Weight', required=True, domain=[
            ('weight', '>=', 0),
            ])

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order.insert(0, ('type', 'ASC'))
        t = cls.__table__()
        cls._sql_constraints += [
             ('journal_weight_uniq', Unique(t, t.journal, t.type),
                 'account_statement_enable_banking.msg_journal_weight_unique'),
        ]

    @fields.depends('type')
    def on_change_type(self):
        if not self.type:
            return
        self.weight = DEFAULT_WEIGHTS.get(self.type)


    @classmethod
    def create(cls, vlist):
        Journal = Pool().get('account.statement.journal')
        Journal._get_weight_cache.clear()
        return super().create(vlist)

    @classmethod
    def write(cls, *args):
        Journal = Pool().get('account.statement.journal')
        Journal._get_weight_cache.clear()
        super().write(*args)

    @classmethod
    def delete(cls, records):
        Journal = Pool().get('account.statement.journal')
        Journal._get_weight_cache.clear()
        return super().delete(records)


class Journal(metaclass=PoolMeta):
    __name__ = 'account.statement.journal'

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
        'Enable Banking Session',
        domain=[
                ('allowed_bank_accounts', '=', Eval('bank_account')),
                ])
    enable_banking_session_allowed_bank_accounts = fields.Function(
        fields.Many2Many('bank.account', None, None, 'Allowed Bank Accounts',
            context={
                'company': Eval('company', -1),
                }, depends={'company'}, readonly=True),
        'on_change_with_enable_banking_session_allowed_bank_accounts')
    one_move_per_origin = fields.Boolean("One Move per Origin",
        help="Check if want to create only one move per origin when post it "
        "even it has more than one line. Else it create one move for eaach "
        "line.")
    max_amount_tolerance = fields.Numeric('Max Amount tolerance',
        domain=[
            ('max_amount_tolerance', '>=', 0),
            ],
        help="In some cases, it is possible to have amounts that vary in X. "
        "This field if set is the maximum of the allowed tolerance. That is, "
        "if value is set when searching for similarities it will look for "
        "equal amounts or with +X, value that has been set here.")
    offset_days_to = fields.Integer('Offset Days To',
        domain=[
            ('offset_days_to', '<=', 20),
            ('offset_days_to', '>=', 0)
            ],
        help='Default offset days in the Bank transaction search. '
        'Allow to not download "to" today, could be set "to" some days before.'
        ' This field could be from 0 to 20. 0 meaning today, other value will '
        'be substracted from today.')
    weights = fields.One2Many('account.statement.journal.weight', 'journal',
        'Weights')
    _get_weight_cache = Cache('account_statement_journal.get_weight')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.bank_account.domain.append(
            If(Eval('enable_banking_session_allowed_bank_accounts', []),
                ('id', 'in',
                    Eval('enable_banking_session_allowed_bank_accounts', [])),
                (),
                ))
        cls._buttons.update({
                'retrieve_enable_banking_session': {},
                'synchronize_statement_enable_banking': {},
                'evaluate_weights': {},
                })

    @classmethod
    def __register__(cls, module_name):
        super().__register__(module_name)
        table = cls.__table_handler__(module_name)

        table.drop_column('acceptable_similarity')
        table.drop_column('similarity_threshold')

    @staticmethod
    def default_validation():
        return 'balance'

    @staticmethod
    def default_one_move_per_origin():
        return False

    @staticmethod
    def default_max_amount_tolerance():
        return 0

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

    def get_weight(self, type):
        key = (self.id, type)
        weight = self._get_weight_cache.get(key)
        if weight is not None:
            return weight
        assert type in DEFAULT_WEIGHTS, "Type '%s' not valid" % type
        value = None
        for weight in self.weights:
            if weight.type == type:
                value = weight.weight
                break
        else:
            value = DEFAULT_WEIGHTS.get(type)
        self._get_weight_cache.set(key, value)
        return value

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
    @ModelView.button
    def evaluate_weights(cls, journals):
        pool = Pool()
        Origin = pool.get('account.statement.origin')
        Payment = pool.get('account.payment')
        Invoice = pool.get('account.invoice')
        MoveLine = pool.get('account.move.line')

        def tuplify(line):
            x = line.related_to
            if (isinstance(x, Payment) and x.line
                    and isinstance(x.line.move_origin, Invoice)):
                x = x.move_origin
            elif isinstance(x, MoveLine) and isinstance(x.move_origin, Invoice):
                x = x.move_origin
            return (str(line.account), str(line.party), line.amount,
                str(x))

        reports = []
        for journal in journals:
            stats = defaultdict(list)
            count = 0
            origins = Origin.search([
                    ('statement.journal', '=', journal.id),
                    ('state', '=', 'posted'),
                    ], order=[
                    ('date', 'DESC'),
                    ('id', 'DESC'),
                    ], limit=100)
            for origin in origins:
                count += 1

                target = sorted([tuplify(x) for x in origin.lines])
                suggestions = []
                for suggestion in origin.suggested_lines:
                    suggestion.update_weight()
                    suggestions.append(suggestion)

                suggestions = sorted(suggestions, key=lambda x: x.weight,
                    reverse=True)
                position = -1
                for suggestion in suggestions:
                    position += 1
                    if suggestion.childs:
                        tuplified = sorted([tuplify(line) for line in
                                suggestion.childs])
                    else:
                        tuplified = [tuplify(suggestion)]
                    if tuplified != target:
                        continue
                    stats[position].append(origin)
                    break
                else:
                    stats[999].append(origin)

            report = f'Journal {journal.name} ({journal.id}) stats on {len(origins)}:\n\n'
            for position in sorted(stats):
                ids = ', '.join(str(x.id) for x in stats[position])
                report += f'  Position {position}: {len(stats[position])}\n'
                report += f'    Origins: {ids}\n'
            reports.append(report)

        raise UserError('\n\n'.join(reports))

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
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        EBConfiguration = pool.get('enable_banking.configuration')
        Statement = pool.get('account.statement')
        StatementOrigin = pool.get('account.statement.origin')
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
        session = json.loads(self.enable_banking_session.session)
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

        # Lock the journal to ensure not duplicate statements or origins
        # as it works with cron + workers.
        Journal.lock([self])

        # We need to create an statement, as is a required field for the origin
        statement = Statement()
        statement.company = self.company
        statement.name = self.name
        statement.date = today
        statement.journal = self
        statement.on_change_journal()
        statement.end_balance = Decimal(0)
        if (not hasattr(statement, 'start_balance')
                or statement.start_balance is None):
            statement.start_balance = Decimal(0)
        statement.start_date = datetime.combine(date_from, datetime.min.time())
        statement.end_date = datetime.combine(date_to, datetime.min.time())
        statement.save()
        Statement.register([statement])

        # Get the data, as we have a limit of transactions every query, we need
        # to do a while loop to get all the transactions
        continuation_key = None
        to_save = []
        total_amount = 0
        last_transaction_date = None
        continuation_key_error = False
        while True:
            if continuation_key:
                query["continuation_key"] = continuation_key
            else:
                query.pop("continuation_key", None)

            r = requests.get(
                f"{URL}/accounts/{account_id}/transactions",
                params=query, headers=base_headers)
            if r.status_code == 200:
                response = r.json()
                continuation_key = response.get('continuation_key')
                last_transaction_date = None
                origins = []
                transactions = response['transactions']
                for transaction in transactions:
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
                    statement_origin.description = (", ".join(transaction.get(
                                'remittance_information', [])))
                    statement_origin.currency = self.currency
                    statement_origin.amount = (
                        transaction.get('transaction_amount', {}).get(
                            'amount', None))
                    if (transaction.get('credit_debit_indicator')
                            and transaction.get('credit_debit_indicator', '')
                            == 'DBIT'):
                        statement_origin.amount = -statement_origin.amount
                    balance_after_transaction = transaction.get(
                        'balance_after_transaction', {})
                    if balance_after_transaction:
                        statement_origin.balance = (
                            balance_after_transaction.get('amount', None))
                    total_amount += statement_origin.amount
                    transaction_date = datetime.strptime(
                        transaction[ebconfig.date_field], '%Y-%m-%d').date()
                    statement_origin.date = transaction_date
                    last_transaction_date = transaction_date
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
                    origins.append(statement_origin)
                    to_save.append(statement_origin)
                StatementOrigin.save(origins)
                if not continuation_key:
                    statement.end_balance = (
                        statement.start_balance + total_amount)
                    statement.save()
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
                    statement.end_balance = (
                        statement.start_balance + total_amount)
                    statement.save()
                    break
            elif r.status_code != 200:
                raise AccessError(
                    gettext('account_statement_enable_banking.'
                        'msg_error_get_statements',
                        error_code=str(r.status_code),
                        error_message=str(r.text)))

        if to_save:
            to_save.sort(reverse=True)
            to_save.sort(key=lambda x: x.date)
            self.set_number(to_save)

            # Get the suggested lines for each origin created
            # Use __queue__ to ensure the Bank lines download and origin
            # creation are done and saved before start to create the
            # suggestions. And use a worker for each origin to ensure that
            # all origin try to search even one fails and can be done in
            # parallel.
            with Transaction().set_context(queue_name=QUEUE_NAME):
                for origin in statement.origins:
                    StatementOrigin.__queue__.search_suggestions([origin])
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

        with Transaction().set_context(queue_name=QUEUE_NAME):
            for journal in Journal.search([
                    ('synchronize_journal', '=', True),
                    ('company.id', '=', company_id),
                    ]):
                cls.__queue__._synchronize_statements_enable_banking(journal)

    @classmethod
    def set_ebsession(cls, eb_session):
        journals = cls.search([
                ('bank_account', '!=', None),
                ('aspsp_name', '!=', None),
                ('aspsp_country', '!=', None),
                ('enable_banking_session', '=', None),
                ])
        to_save = []
        for journal in journals:
            if not eb_session.encrypted_session:
                continue
            if journal.bank_account in eb_session.allowed_bank_accounts:
                journal.enable_banking_session = eb_session
                to_save = [journal]
                break
        if to_save:
            cls.save(to_save)


class Cron(metaclass=PoolMeta):
    __name__ = 'ir.cron'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.method.selection.extend([
            ('account.statement.journal|synchronize_enable_banking_journals',
                "Synchronize Enable Banking Journals"),
            ])
