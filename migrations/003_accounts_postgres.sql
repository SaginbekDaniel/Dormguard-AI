-- Role extension preserves existing identities, FKs, uniqueness and RLS.
ALTER TABLE public.users DROP CONSTRAINT users_role_check;
ALTER TABLE public.users ADD CONSTRAINT users_role_check CHECK(role IN ('director','deputy_director','technical_admin','super_admin'));
ALTER TABLE public.users ADD COLUMN display_name TEXT NOT NULL DEFAULT '' CHECK(length(display_name)<=80);
