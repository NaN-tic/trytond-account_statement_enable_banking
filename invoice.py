# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from collections import defaultdict

from trytond.pool import Pool, PoolMeta
from trytond.tools import grouped_slice, reduce_ids
from trytond.transaction import Transaction


class Invoice(metaclass=PoolMeta):
    __name__ = 'account.invoice'

    # Need to refactorize the fleid function, to allow to have all
    # the statement lines from ana origin in the same account move.
    @classmethod
    def get_lines_to_pay(cls, invoices, name):
        pool = Pool()
        Line = pool.get('account.move.line')

        lines = super().get_lines_to_pay(invoices, name)
        for invoice_id, lines_id in lines.items():
            invoice = cls(invoice_id)
            new_lines = []
            for line in Line.search([('id', 'in', lines_id)]):
                if line.move.origin == invoice and line.party == invoice.party:
                    new_lines.append(line.id)
            lines[invoice_id] = new_lines
        return lines

    # Need to refactorize the fleid function, to allow to have all
    # the statement lines from ana origin in the same account move.
    def get_reconciliation_lines(self, name):
        pool = Pool()
        Line = pool.get('account.move.line')

        lines = super().get_reconciliation_lines(name)
        if not lines:
            return lines
        new_lines = set()
        reconciliation_lines_to_pay = [x.reconciliation
            for x in self.lines_to_pay if x.reconciliation]
        for line in Line.search([('id', 'in', lines)]):
            if (line.party == self.party
                    and line.reconciliation in reconciliation_lines_to_pay):
                new_lines.add(line)
        return [x.id for x in sorted(new_lines, key=lambda x: x.date)]
