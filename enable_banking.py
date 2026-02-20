# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import cryptography
import json
import requests
from requests.exceptions import ConnectionError
from datetime import datetime
from cryptography.fernet import Fernet

from trytond.pool import Pool
from trytond.model import ModelSingleton, ModelSQL, ModelView, fields
from trytond.i18n import gettext
from trytond.exceptions import UserError
from trytond.config import config
from trytond.transaction import Transaction
from .common import get_base_header, URL
from trytond.report import Report

FERNET_KEY = config.get('cryptography', 'fernet_key')


class EnableBankingConfiguration(ModelSingleton, ModelSQL, ModelView):
    "Enable Banking Configuration"
    __name__ = 'enable_banking.configuration'

    redirecturl = fields.Char("Redirect URL")
    date_field = fields.Selection([
        ('booking_date', 'Booking Date'),
        ('transaction_date', 'Transaction Date'),
        ('value_date', 'Value Date'),
    ], "Date Field", help='Choose which date to use when importing statements')
    offset = fields.Integer("Offset Days",
        domain=[
            ('offset', '>=', 0)
            ],
        help="Offset in days to apply when importing statements manually")

    @classmethod
    def default_date_field(cls):
        return 'transaction_date'

    @classmethod
    def default_offset(cls):
        return 10

    @classmethod
    def __setup__(cls):
        super(EnableBankingConfiguration, cls).__setup__()
        cls._buttons.update({
            'test_connection': {}
        })

    @classmethod
    @ModelView.button
    def test_connection(cls, aspsps):
        base_headers = get_base_header()

        try:
            r = requests.get(f"{URL}/application",
                headers=base_headers)
        except ConnectionError as e:
            raise UserError(gettext(
                'account_statement_enable_banking.msg_connection_test_error',
                error_message=str(e)))

        if r.status_code == 200:
            raise UserError(gettext(
                'account_statement_enable_banking.msg_connection_test_ok'))
        else:
            raise UserError(gettext(
                'account_statement_enable_banking.msg_connection_test_error',
                error_message=r.text))


class EnableBankingSession(ModelSQL, ModelView):
    "Enable Banking Session"
    __name__ = 'enable_banking.session'

    session_id = fields.Char("Session ID", readonly=True)
    valid_until = fields.DateTime('Valid Until', readonly=True)
    encrypted_session = fields.Binary('Encrypted Session')
    session = fields.Function(fields.Text('Session'),
        'get_session', 'set_session')
    aspsp_name = fields.Char("ASPSP Name", readonly=True)
    aspsp_country = fields.Char("ASPSP Country", readonly=True)
    bank = fields.Many2One('bank', "Bank", readonly=True)
    allowed_bank_accounts = fields.Function(fields.Many2Many(
            'bank.account', None, None, 'Allowed Bank Accounts',readonly=True),
        'get_allowed_bank_accounts', searcher='search_allowed_bank_accounts')

    @classmethod
    def __register__(cls, module_name):
        cursor = Transaction().connection.cursor()
        table = cls.__table_handler__(module_name)
        exist_company = table.column_exist('company')
        sql_table = cls.__table__()

        # TODO: 'session' migration could be removed in v7.6
        session = table.column_exist('session')

        super().__register__(module_name)

        if exist_company:
            table.drop_column('company')
        if session:
            cursor.execute(*sql_table.select(sql_table.id, sql_table.session))
            for sessions in cursor.fetchall():
                session_id = sessions[0]
                session_txt = sessions[1]
                encrypted_session = None
                if session_txt:
                    session_txt = session_txt.replace("'", '"').replace(
                        'None', 'null').replace('True', 'true').replace(
                        'False', 'false')
                    fernet = cls.get_fernet_key()
                    if not fernet:
                        continue
                    encrypted_session = fernet.encrypt(session_txt.encode())
                cursor.execute(*sql_table.update(
                        columns=[sql_table.encrypted_session],
                        values=[encrypted_session],
                        where=sql_table.id == session_id))

            table.drop_column('session')

    def get_session(self, name):
        session = self._get_session(name)

        if not session:
            return None

        return session

    def _get_session(self, name=None):
        if (not self.encrypted_session
                or not config.getboolean('database', 'production', default=False)):
            return

        fernet = self.get_fernet_key()
        if not fernet:
            return

        try:
            return fernet.decrypt(self.encrypted_session).decode()
        except cryptography.fernet.InvalidToken:
            raise

    @classmethod
    def set_session(cls, eb_sessions, name, value):
        encrypted_session = None
        if value:
            fernet = cls.get_fernet_key()
            if not fernet:
                return
            encrypted_session = fernet.encrypt(value.encode())
        cls.write(eb_sessions, {'encrypted_session': encrypted_session})

    @classmethod
    def get_fernet_key(cls):
        if not FERNET_KEY:
            raise UserError(gettext(
                    'account_statement_enable_banking.msg_missing_fernet_key'))
        else:
            return Fernet(FERNET_KEY)

    @classmethod
    def _get_bank_accounts(cls, enable_banking_session):
        pool = Pool()
        BankNumber = pool.get('bank.account.number')
        BankAccount = pool.get('bank.account')

        if not enable_banking_session.session:
            return []
        session = json.loads(enable_banking_session.session)
        accounts = session.get('accounts') if session else {}
        iban_numbers = [x.get('account_id', {}).get('iban') for x in accounts]
        domain = [
            ('type', '=', 'iban'),
            ('number_compact', 'in', iban_numbers),
            ]
        numbers = BankNumber.search(domain)
        if hasattr(BankAccount, 'companies'):
            company_id = Transaction().context.get('company', -1)
            result = []
            for num in numbers:
                if num.account is None or not num.account.active:
                    continue
                if ((company_id in [c.id for c in num.account.companies])
                        and (company_id in [c.id
                            for owner in num.account.owners
                            for c in owner.companies])):
                    result.append(num.account.id)
            return result
        else:
            return [x.account.id for x in numbers
                if x.account is not None and x.account.active]

    def get_allowed_bank_accounts(self, name=None):
        pool = Pool()
        EnableBankingSession = pool.get('enable_banking.session')

        return EnableBankingSession._get_bank_accounts(self)

    @classmethod
    def search_allowed_bank_accounts(cls, name, clause):
        _, operator, value = clause
        ids = []
        enable_banking_sessions = cls.search([])
        if value:
            session_iban_numbers = {}
            for enable_banking_session in enable_banking_sessions:
                session_iban_numbers[enable_banking_session.id] = (
                    cls._get_bank_accounts(enable_banking_session))

            for session, bank_accounts in session_iban_numbers.items():
                if operator == '=':
                    if value in bank_accounts:
                        ids.append(session)
                elif operator == '!=':
                    if value not in bank_accounts:
                        ids.append(session)
                elif operator == 'in':
                    if all(x in bank_accounts for x in value):
                        ids.append(session)
                else:
                    if not all(x in bank_accounts for x in value):
                        ids.append(session)
        else:
            ids = [x.id for x in enable_banking_sessions]

        return [('id', 'in', ids)]

    @property
    def session_expired(self):
        if self.valid_until and self.valid_until >= datetime.now():
            return False
        return True


class EnableBankingSessionOK(Report):
    "Enable Banking Session OK"
    __name__ = 'enable_banking.session_ok'


class EnableBankingSessionKO(Report):
    "Enable Banking Session KO"
    __name__ = 'enable_banking.session_ko'
