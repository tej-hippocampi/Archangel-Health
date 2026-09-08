# Reproduce the form investigation

Run from the code repository root with Node 24. Install react@18.3.1, react-dom@18.3.1, jsdom@26.1.0, and esbuild@0.25.0 into a temporary directory, not the product checkout. Point ARCHANGEL_UX_DEPS and NODE_PATH at that directory's node_modules. The default build dependency directory is /tmp/archangel-onboarding-ux-audit/node_modules.

Run `node docs/prd/onboarding-ux-evidence/build_form_probe.cjs`, then `node docs/prd/onboarding-ux-evidence/run_form_probe.cjs current` and the same with `stable`. Set ARCHANGEL_REPO if running outside the code checkout. No browser, production API, or account access is used.

The build copies current components into generated/ and hoists Group only in the stable experiment. The scripts assume the inspected Group signature; adapt the harness to a changed implementation rather than overwriting production source. Tested source snapshots and hashes are included for reproducibility. The scripts report diagnostic failures in JSON; exit zero means execution completed, not that every expectation passed.

Current and stable result files are the final 29-check run. JSDOM has no layout/scroll engine: real-browser pixel scroll, mobile keyboard, tab navigation, and screen-reader tests remain release requirements. Package installation/build steps are local-only; the only download used for deployed matching was the public JavaScript asset named in source_manifest.json.
