---
title: Corrections
description: How to report an error in a record, what happens next, and what this database does and does not claim.
---

## What a record claims

Each record restates what one named news outlet reported on the date shown, with the sentences that document the prior record and release status quoted verbatim from that article. A record does not claim that the underlying facts are true, only that the outlet reported them. News reports of arrests rely on police and prosecutor statements. Charges get dropped, cases end in acquittal, and outcomes change after publication. A record's outcome is the outcome at the time the article was written unless a later source was added.

Classification is automated. The prior-record and release-status fields are backed by quotes checked against the article text, and every stated count must appear in the article, but errors in either direction remain possible, including a wrong person, a miscounted record, or two people with one name treated as one.

## What is not included

- Anyone under 18, whatever the article reports.
- Anyone the article does not report as arrested, charged, indicted, convicted, or sentenced. Suspects who are wanted or at large are not stored.
- Stories where the article does not state at least five prior arrests, five prior convictions, or three prior felony convictions. Counts are never estimated.
- Stories reported by only one outlet, unless that outlet is a national news organization or a metro daily with a professional newsroom. Other stories are published only after a second outlet's report is found.

## Reporting an error

Use the flag (⚑) on any record. It opens a form with the record id filled in. Say what is wrong and, if you can, link to a source. Reports are public on the issue tracker, get an automated first assessment, and are decided by the maintainer.

If you are the person named in a record, or represent them, and would rather not post publicly, write to **[email address to be added]**. Say which record and what is wrong.

## What happens next

- Reports are acknowledged within three business days and resolved within ten.
- A record that is wrong is corrected; a record that should not exist is removed. Either way the change is logged in the repository's history.
- A removed record keeps its id. Its address shows only the date and reason for removal, so a shared link never resolves to a different person.
- If the cited article has been taken down or changed so that it no longer supports the record, the record is removed.
- Corrections do not require proof of harm. A documented factual error is enough.
