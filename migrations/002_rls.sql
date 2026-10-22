-- Backend-only browser boundary. No browser grants, including for officials:
-- HTTP authorization mediates sensitive reads and writes. The service database
-- principal owns these tables; never expose its credentials to clients.
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['schema_migrations','users','sessions','stream_tickets','cameras','camera_health','worker_health','incidents','incident_keys','events','evidence','telegram_bindings','telegram_links','notification_outbox','notification_attempts','audit_logs'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC', t);
    IF EXISTS (SELECT FROM pg_roles WHERE rolname='anon') THEN
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM anon',t);
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname='authenticated') THEN
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM authenticated',t);
    END IF;
  END LOOP;
END $$;
CREATE OR REPLACE FUNCTION public.dg_role() RETURNS TEXT
LANGUAGE SQL STABLE SECURITY DEFINER SET search_path=pg_catalog,public
AS $$ SELECT role FROM public.users WHERE id=current_setting('request.jwt.claim.sub',true) AND enabled=1 $$;
REVOKE ALL ON FUNCTION public.dg_role() FROM PUBLIC;
CREATE OR REPLACE FUNCTION public.dg_append_only() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path=pg_catalog,public
AS $$ BEGIN RAISE EXCEPTION 'append-only record'; END $$;
REVOKE ALL ON FUNCTION public.dg_append_only() FROM PUBLIC;
DO $$
DECLARE client_role TEXT;
BEGIN
  FOREACH client_role IN ARRAY ARRAY['anon','authenticated'] LOOP
    IF EXISTS (SELECT FROM pg_roles WHERE rolname=client_role) THEN
      EXECUTE format('REVOKE ALL ON FUNCTION public.dg_role() FROM %I',client_role);
      EXECUTE format('REVOKE ALL ON FUNCTION public.dg_append_only() FROM %I',client_role);
    END IF;
  END LOOP;
END $$;
DROP TRIGGER IF EXISTS audit_append_only ON public.audit_logs;
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON public.audit_logs FOR EACH ROW EXECUTE FUNCTION public.dg_append_only();
DROP TRIGGER IF EXISTS evidence_append_only ON public.evidence;
CREATE TRIGGER evidence_append_only BEFORE UPDATE OR DELETE ON public.evidence FOR EACH ROW EXECUTE FUNCTION public.dg_append_only();
