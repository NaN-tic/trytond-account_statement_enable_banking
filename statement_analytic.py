# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from trytond.pool import Pool, PoolMeta
from trytond.model import fields
from trytond.pyson import Eval
from trytond.modules.analytic_account import AnalyticMixin


class Line(AnalyticMixin, metaclass=PoolMeta):
    __name__ = 'account.statement.line'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.analytic_accounts.domain = [
            ('company', '=', Eval('company', -1)),
            ]
        cls.analytic_accounts.states = {
            'readonly': Eval('statement_state') != 'draft',
            }

    def get_move_line(self):
        pool = Pool()
        AnalyticLine = pool.get('analytic_account.line')

        move_line = super().get_move_line()

        if not hasattr(self, 'analytic_accounts'):
            return move_line

        if move_line and self.analytic_accounts:
            for analytic_account in self.analytic_accounts:
                if analytic_account.account:
                    account = analytic_account.account
                    if move_line.account != self.account:
                        continue
                    analytic_line = AnalyticLine()
                    analytic_line.debit = move_line.debit
                    analytic_line.credit = move_line.credit
                    analytic_line.account = account
                    analytic_line.date = self.date
                    if not hasattr(move_line, 'analytic_lines'):
                        move_line.analytic_lines = (analytic_line,)
                    else:
                        move_line.analytic_lines += (analytic_line,)
        return move_line


class Origin(metaclass=PoolMeta):
    __name__ = 'account.statement.origin'

    def apply_analytic_accounts(self, suggestion, record):
        pool = Pool()
        AnalyticAccountEntry = pool.get('analytic.account.entry')

        if not record:
            return
        new_entries = []
        for entry in getattr(record, 'analytic_accounts', []):
            new_entry = AnalyticAccountEntry()
            new_entry.root = entry.root
            new_entry.account = entry.account
            new_entries.append(new_entry)
        suggestion.analytic_accounts = new_entries

    def get_suggestion_from_move_line(self, line):
        suggestion = super().get_suggestion_from_move_line(line)
        self.apply_analytic_accounts(suggestion, record=line)
        return suggestion

    def _suggest_origin_key(self, line):
        key = super()._suggest_origin_key(line)
        accounts = sorted([x.account and x.account.id or -1 for x in
                getattr(line, 'analytic_accounts', [])])
        key += (tuple(accounts),)
        return key

    def get_suggestion_from_origin_key(self, origin, key):
        pool = Pool()
        AnalyticAccount = pool.get('analytic_account.account')
        AnalyticAccountEntry = pool.get('analytic.account.entry')

        suggestion = super().get_suggestion_from_origin_key(origin, key)
        # TODO: If other modules inherit _suggest_origin_key, we cannot
        # assume that the 3rd element is always the analytic accounts
        accounts = AnalyticAccount.browse([x for x in key[2] if x != -1])
        new_entries = []
        for account in accounts:
            new_entry = AnalyticAccountEntry()
            new_entry.root = account.root
            new_entry.account = account
            new_entries.append(new_entry)
        suggestion.analytic_accounts = new_entries
        return suggestion


class OriginSuggestedLine(AnalyticMixin, metaclass=PoolMeta):
    __name__ = 'account.statement.origin.suggested.line'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.analytic_accounts.domain = [
            ('company', '=', Eval('company', -1)),
            ]
        cls.analytic_accounts.states = {
            'readonly': Eval('origin.state') != 'registered',
            }

    def get_statement_line(self):
        pool = Pool()
        AnalyticAccountEntry = pool.get('analytic.account.entry')

        line = super().get_statement_line()
        new_entries = []
        for entry in getattr(self, 'analytic_accounts', []):
            new_entry = AnalyticAccountEntry()
            new_entry.root = entry.root
            new_entry.account = entry.account
            new_entries.append(new_entry)
        line.analytic_accounts = new_entries
        return line


class AnalyticAccountEntry(metaclass=PoolMeta):
    __name__ = 'analytic.account.entry'

    @classmethod
    def _get_origin(cls):
        origins = super()._get_origin()
        return origins + ['account.statement.line',
            'account.statement.origin.suggested.line']

    @fields.depends('origin')
    def on_change_with_company(self, name=None):
        pool = Pool()
        StatementLine = pool.get('account.statement.line')
        SuggestedLine = pool.get('account.statement.origin.suggested.line')

        company = super().on_change_with_company(name)
        if (isinstance(self.origin, StatementLine)
                or isinstance(self.origin, SuggestedLine)):
            company = self.origin.company.id
        return company

    @classmethod
    def search_company(cls, name, clause):
        domain = super().search_company(name, clause),
        return ['OR',
            domain,
            (('origin.company',) + tuple(clause[1:]) +
                ('account.statement.line',)),
            (('origin.company',) + tuple(clause[1:]) +
                ('account.statement.origin.suggested.line',)),
            ]
