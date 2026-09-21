# This file is part account_statement_enable_banking module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.pool import Pool
from . import enable_banking
from . import journal
from . import statement
from . import routes

# We need to set the routes file here to activate the routes in tryton
# if we dont have the routes file here, the routes will not be activated
# and tryton will reuturn a 405 Method Not Allowed error
__all__ = ['register', 'routes']

def register():
    Pool.register(
        enable_banking.EnableBankingConfiguration,
        enable_banking.EnableBankingSession,
        journal.Journal,
        journal.Cron,
        statement.Origin,
        statement.RetrieveEnableBankingSessionStart,
        statement.RetrieveEnableBankingSessionSelect,
        statement.OriginSynchronizeStatementEnableBankingAsk,
        module='account_statement_enable_banking', type_='model')
    Pool.register(
        statement.RetrieveEnableBankingSession,
        statement.OriginSynchronizeStatementEnableBanking,
        module='account_statement_enable_banking', type_='wizard')
    Pool.register(
        enable_banking.EnableBankingSessionOK,
        enable_banking.EnableBankingSessionKO,
        module='account_statement_enable_banking', type_='report')
