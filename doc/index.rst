Account Statement Enable Banking Module
#######################################

Deferred bank review
====================

The regular synchronization imports new movements as ``Pending Review``.
This does not prevent reconciliation or posting. Both regular synchronization
and deferred review compare movements within the statement journal. A changed
reference or changed data immediately flags all matching origins as
``With Discrepancies``, without waiting for the stabilization margin.

For each journal, set ``Review Start Date`` to the first date to check and
``Review Margin Days`` to the bank's stabilization delay (three days by
default). Configure a scheduled action with method
``Review Enable Banking Journals`` for the relevant companies. It processes
journals with automatic synchronization enabled and a review start date.
``Review Statements`` runs the same verification manually.

The review downloads the full period through today minus the margin,
inclusive. Subsequent runs resume from ``Reviewed Until``, overlapping by the
configuration's ``Offset Days``. They catch up after missed scheduled runs.
The checkpoint advances only after a complete download and comparison;
pagination errors abort the review instead of skipping dates.

Within the same journal, compare reference, date, amount and information.
When either movement has no information, compare descriptions instead.
During deferred review, exact matches are reviewed automatically, unless they
have an open discrepancy. New movements with no candidates are also reviewed
immediately. An exact match does not hide other partial matches: previously
unlinked candidates are flagged without creating another origin.
A shared reference or otherwise identical movement with a different reference
creates a new origin and flags all matching candidates. Missing movements
are flagged after the full period has been fetched.

The origin's ``Review`` tab contains the explanation and related origins.
``Mark as Reviewed`` resolves an individual discrepancy. For a new conflict
origin without statement lines, it also cancels the origin. Posting a
conflicting origin with lines resolves its review automatically. Posting a
pending origin does not resolve its review.

Resolved and cancelled variants remain available for comparison, so repeated
downloads do not recreate them. Resolving one origin does not resolve its
related origins. Known conflict pairs are not reopened on repeated downloads,
but a new bank variant reopens the review of all its matching origins.
Review imports use separate technical statements and do not advance the
regular download cursor.
