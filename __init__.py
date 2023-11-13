# This file is part account_statement_enable_banking module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.pool import Pool
from . import account
from . import enable_banking
from . import journal
from . import routes
from . import statement
from . import statement_aeb43

def register():
    Pool.register(
        account.Move,
        account.MoveLine,
        enable_banking.EnableBankingConfiguration,
        enable_banking.EnableBankingSession,
        journal.Journal,
        statement.Statement,
        statement.Line,
        statement.Origin,
        statement.OriginSuggestedLine,
        statement.SynchronizeStatementEnableBankingStart,
        statement.Cron,
        module='account_statement_enable_banking', type_='model')
    Pool.register(
        statement.SynchronizeStatementEnableBanking,
        module='account_statement_enable_banking', type_='wizard')
    Pool.register(
        enable_banking.EnableBankingSessionOK,
        enable_banking.EnableBankingSessionKO,
        module='account_statement_enable_banking', type_='report')
    Pool.register(
        statement_aeb43.ImportStatement,
        depends=['account_statement_aeb43'],
        module='account_statement_enable_banking', type_='wizard')
