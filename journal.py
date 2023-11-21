# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import requests
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from trytond.pool import Pool, PoolMeta
from trytond.model import ModelView, fields
from trytond.pyson import Eval, Id
from trytond.config import config
from trytond.i18n import gettext
from trytond.model.exceptions import AccessError
from .common import get_base_header


class Journal(metaclass=PoolMeta):
    __name__ = 'account.statement.journal'

    similarity_threshold = fields.Integer('Similarity Threshold', required=True,
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

    @staticmethod
    def default_similarity_threshold():
        return 5

    @staticmethod
    def default_acceptable_similarity():
        return 8

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
        return [
            'balance_after_transaction',
            'transaction_amount',
            'credit_debit_indicator',
            'status'
            ]

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
        statement.end_date = Date.today()
        statement.start_date = datetime.now(timezone.utc) - timedelta(
            days=ebconfig.offset)
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
                        if value is None or key in self._keys_not_needed():
                            continue
                        if isinstance(value, str):
                            information_dict[key] = value
                        if isinstance(value, bytes):
                            information_dict[key] = str(value)
                        if isinstance(value, dict):
                            for k, v in value.items():
                                if value is None:
                                    continue
                                tag = "%s - %s" % (key, k)
                                information_dict[tag] = str(value)
                        if isinstance(value, list):
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
                        error=str(r.status_code),
                        error_message=str(r.text)))

        to_save.sort(key=lambda x: x.date)
        # The set number function save the origins
        self.set_number(to_save)

        # Get the suggested lines for each origin created
        StatementOrigin._search_reconciliation(statement.origins)

    @classmethod
    def synchronize_enable_banking_journals(cls):
        pool = Pool()
        Journal = pool.get('account.statement.journal')
        for journal in Journal.search([('synchronize_journal', '=', True)]):
            journal.synchronize_statements_enable_banking()
