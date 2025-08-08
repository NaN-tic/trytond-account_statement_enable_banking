# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import requests
import json
from datetime import datetime
from cryptography.fernet import Fernet

from trytond.pool import Pool
from trytond.model import ModelSingleton, ModelSQL, ModelView, fields
from trytond.i18n import gettext
from trytond.exceptions import UserError
from trytond.config import config
from trytond.transaction import Transaction
from .common import get_base_header
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
        r = requests.get(f"{config.get('enable_banking', 'api_origin')}/application",
            headers=base_headers)
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
        'get_allowed_bank_accounts')

    @classmethod
    def __register__(cls, module_name):
        cursor = Transaction().connection.cursor()
        table = cls.__table_handler__(module_name)
        sql_table = cls.__table__()

        session = table.column_exist('session')

        super().__register__(module_name)

        if session:
            cursor.execute(*sql_table.select(sql_table.id, sql_table.session))
            for sessions in cursor.fetchall():
                session_id = sessions[0]
                session_txt = sessions[1]
                encrypted_session = None
                if session_txt:
                    fernet = cls.get_fernet_key()
                    if not fernet:
                        continue
                    encrypted_session = fernet.encrypt(session_txt.encode())
                cursor.execute(*sql_table.update(
                        columns=[sql_table.encrypted_session],
                        values=[encrypted_session],
                        where=sql_table.id == session_id))

            table.drop_column('session')

    @classmethod
    def get_session(cls, eb_sessions, name=None):
        psessions = []
        for eb_session in eb_sessions:
            session = eb_session._get_session(name)
            if not session:
                continue
            psessions.append(session)

        if not psessions:
            return {x.id:None for x in eb_sessions}

        return {
            eb_session.id: psession if psession else None
            for (eb_session, psession) in zip(eb_sessions, psessions)
            }

    def _get_session(self, name=None):
        if not self.encrypted_session:
            return None
        fernet = self.get_fernet_key()
        if not fernet:
            return None
        return fernet.decrypt(self.encrypted_session).decode()

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

    def get_allowed_bank_accounts(self, name=None):
        pool = Pool()
        BankNumber = pool.get('bank.account.number')

        if not self.encrypted_session:
            return []
        # To ensure the text is converted correctly as a json to dict, change
        # some things
        session_text = self.session.replace("'", '"').replace("None", "null")
        session_text = session_text.replace("True", "true").replace("False",
            "false")
        session = json.loads(session_text)
        accounts = session.get('accounts') if session else {}
        iban_numbers = [x.get('account_id', {}).get('iban') for x in accounts]
        numbers = BankNumber.search([
                ('type', '=', 'iban'),
                ['OR',
                    ('number', 'in', iban_numbers),
                    ('number_compact', 'in', iban_numbers),
                    ],
                ])
        return [x.account.id for x in numbers
            if x.account is not None and x.account.active]

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
