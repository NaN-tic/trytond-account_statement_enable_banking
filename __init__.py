# This file is part account_statement_enable_banking module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.pool import Pool
from . import statement
from . import enable_banking
from . import routes

def register():
    Pool.register(
        statement.Origin,
        statement.Journal,
        statement.SynchronizeStatementEnableBankingStart,
        statement.Cron,
        enable_banking.EnableBankingConfiguration,
        enable_banking.EnableBankingSession,
        module='account_statement_enable_banking', type_='model')
    Pool.register(
        statement.SynchronizeStatementEnableBanking,
        module='account_statement_enable_banking', type_='wizard')
    Pool.register(
        enable_banking.EnableBankingSessionOK,
        enable_banking.EnableBankingSessionKO,
        module='account_statement_enable_banking', type_='report')
