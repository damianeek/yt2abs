PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

.PHONY: test e2e e2e-playlist e2e-security e2e-service e2e-discord e2e-notify-settings e2e-ntfy e2e-retention e2e-webui e2e-firefox check run progress ext-test ext-lint ext-build ext-sign

test:                      ## unit tests (stdlib, no network)
	$(PYTHON) -m unittest discover -s tests -t . -v

e2e-security:              ## real uvicorn: open LAN surface, rate limit, XFF trust
	$(PYTHON) tests/e2e_security.py

e2e:                       ## E2E SponsorBlock on a real video (network + yt-dlp + ffmpeg)
	YT2ABS_E2E=1 $(PYTHON) tests/e2e_sponsorblock.py

e2e-playlist:              ## E2E playlist from the phone (offline: yt-dlp fake + ABS stub)
	$(PYTHON) tests/e2e_playlist.py

e2e-service:               ## E2E service (requires a running yt2abs; URL in YT2ABS_BASE_URL)
	$(PYTHON) tests/e2e_service.py $(E2E_URL)

e2e-discord:               ## E2E Discord notifications (DISCORD_WEBHOOK_URL, network, yt-dlp)
	$(PYTHON) tests/e2e_discord.py

e2e-notify-settings:       ## E2E WebUI event settings -> real Discord (without yt-dlp)
	$(PYTHON) tests/e2e_notify_settings.py

e2e-ntfy:                  ## E2E ntfy notifications (local HTTP sink, no network)
	$(PYTHON) tests/e2e_ntfy.py

e2e-retention:             ## E2E retention/update (runs in the yt2abs container; requires ABS)
	$(PYTHON) tests/e2e_retention.py

e2e-retention-restart:     ## E2E config.yaml change + container restart (host; YT2ABS_CONTAINER, YT2ABS_CONFIG_HOST)
	$(PYTHON) tests/e2e_retention_restart.py

e2e-webui:                 ## E2E WebUI/API: import, keep, retention, failed (BASE_URL + ABS)
	$(PYTHON) tests/e2e_webui.py prepare

e2e-firefox:               ## E2E Firefox extension (Firefox + geckodriver + selenium + ABS)
	$(PYTHON) tests/e2e_firefox_extension.py

ext-test:                  ## Firefox extension tests (node --test, no dependencies)
	cd extension && node --test tests/*.test.js

ext-lint:                  ## web-ext lint for the extension (requires Node + npx)
	cd extension && npx --yes web-ext@10 lint --source-dir src

ext-build:                 ## build extension/dist/yt2abs-<version>.zip
	cd extension && npx --yes web-ext@10 build --source-dir src --artifacts-dir dist --overwrite-dest

ext-sign:                  ## sign unlisted on AMO (WEB_EXT_API_KEY / WEB_EXT_API_SECRET)
	cd extension && npx --yes web-ext@10 sign --source-dir src --artifacts-dir dist --channel=unlisted

run:                       ## run the API + worker locally (port 8080)
	$(PYTHON) -m yt2abs

check:                     ## validate config.example.yaml
	$(PYTHON) -m yt2abs.cli check --config config.example.yaml

progress:                  ## git: commits and uncommitted files
	@echo "== latest commits =="; git log --oneline --decorate -15
	@echo; echo "== uncommitted =="; git status --short
