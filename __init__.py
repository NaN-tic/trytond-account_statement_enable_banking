# This file is part account_statement_enable_banking module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.pool import Pool
from . import account
from . import enable_banking
from . import journal
from . import statement
from . import statement_aeb43
from . import statement_analytic
from . import invoice
from . import account_bank
from . import move
from . import routes

# We need to set the routes file here to activate the routes in tryton
# if we dont have the routes file here, the routes will not be activated
# and tryton will reuturn a 405 Method Not Allowed error
__all__ = ['register', 'routes']

def register():
    Pool.register(
        account.Move,
        account.MoveLine,
        enable_banking.EnableBankingConfiguration,
        enable_banking.EnableBankingSession,
        journal.Journal,
        journal.Cron,
        statement.Statement,
        statement.Line,
        statement.Origin,
        statement.OriginSuggestedLine,
        statement.AddMultipleInvoicesStart,
        statement.AddMultipleMoveLinesStart,
        statement.SynchronizeStatementEnableBankingStart,
        statement.OriginSynchronizeStatementEnableBankingAsk,
        invoice.Invoice,
        module='account_statement_enable_banking', type_='model')
    Pool.register(
        statement.AddMultipleInvoices,
        statement.AddMultipleMoveLines,
        statement.SynchronizeStatementEnableBanking,
        statement.OriginSynchronizeStatementEnableBanking,
        module='account_statement_enable_banking', type_='wizard')
    Pool.register(
        enable_banking.EnableBankingSessionOK,
        enable_banking.EnableBankingSessionKO,
        module='account_statement_enable_banking', type_='report')
    Pool.register(
        statement_aeb43.ImportStatement,
        depends=['account_statement_aeb43'],
        module='account_statement_enable_banking', type_='wizard')
    Pool.register(
        account_bank.CompensationMove,
        depends=['account_bank'],
        module='account_statement_enable_banking', type_='wizard')
    Pool.register(
        statement_analytic.Line,
        statement_analytic.Origin,
        statement_analytic.OriginSuggestedLine,
        statement_analytic.AnalyticAccountEntry,
        depends=['analytic_account'],
        module='account_statement_enable_banking', type_='model')
    Pool.register(
        move.Move,
        depends=['account_es'],
        module='account_statement_enable_banking', type_='model')
