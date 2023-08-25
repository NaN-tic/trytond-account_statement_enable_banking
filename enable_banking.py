# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import requests

from trytond.model import ModelSingleton, ModelSQL, ModelView, fields
from trytond.i18n import gettext
from trytond.exceptions import UserError
from trytond.config import config
from .common import get_base_header
from trytond.report import Report


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
        help="Offset in days to apply when importing statements")

    @classmethod
    def default_date_field(cls):
        return 'booking_date'

    @classmethod
    def default_offset(cls):
        return 2

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
                error_code=r.status_code,
                error_message=r.text))


class EnableBankingSession(ModelSQL, ModelView):
    "Enable Banking Session"
    __name__ = 'enable_banking.session'

    company = fields.Many2One('company.company', "Company", required=True)
    session_id = fields.Char("Session ID", readonly=True)
    valid_until = fields.DateTime('Valid Until', readonly=True)
    session = fields.Text("Session", readonly=True)
    aspsp_name = fields.Char("ASPSP Name", readonly=True)
    aspsp_country = fields.Char("ASPSP Country", readonly=True)
    bank = fields.Many2One('bank', "Bank", readonly=True)


class EnableBankingSessionOK(Report):
    "Enable Banking Session OK"
    __name__ = 'enable_banking.session_ok'


class EnableBankingSessionKO(Report):
    "Enable Banking Session KO"
    __name__ = 'enable_banking.session_ko'
