-- Restored staging tables may lack PK; required for scan_results_v2 FK.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'public.scan_jobs'::regclass
      AND contype = 'p'
  ) THEN
    ALTER TABLE public.scan_jobs ADD PRIMARY KEY (id);
  END IF;
END $$;
