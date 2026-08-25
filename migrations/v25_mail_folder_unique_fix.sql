-- v25_mail_folder_unique_fix.sql
-- Fix mail_folders uniqueness. The original uq_acct_role_sys
-- (account_id, role, is_system) allowed only ONE row per (account, role,
-- is_system) — but every custom folder shares role='custom', so the 2nd
-- custom folder for an account collided on e.g. '<acct>-custom-0'. Gmail
-- exposes many folders (All Mail, Important, Starred, [Gmail], labels), so
-- folder sync hit a Duplicate entry error and rolled the whole sync back.
--
-- Re-key folder uniqueness to the server path instead. NULL imap_path (seeded
-- system folders pre-sync, the local Outbox, user-created custom folders) may
-- repeat — MySQL treats NULLs as distinct in a UNIQUE index. System folders
-- stay one-per-role via the idempotent app-side seed.
--
-- Run on ats_staging first, then ats_basic (prod) once the feature promotes.

ALTER TABLE mail_folders DROP INDEX uq_acct_role_sys;
ALTER TABLE mail_folders ADD UNIQUE KEY uq_acct_imap_path (account_id, imap_path);
