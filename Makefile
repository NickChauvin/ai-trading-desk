.PHONY: test shadow paper stocks review review-claude kill demo

test:            ## run the offline test suite
	python -m unittest discover -s tests -v

demo:            ## invented fixtures, no API keys, no network
	python demos/offline_demo.py

shadow-bounded:  ## one shadow cycle, hard ceilings, no paper ledger
	python scripts/bounded_shadow_trial.py

shadow:          ## one full cycle, journal + alerts, no position
	python run_crypto.py --shadow --once

paper:           ## paper trading, every 60 seconds
	python run_crypto.py

stocks:          ## Alpaca paper account, every 15 minutes while open
	python run_stocks.py

review:          ## weekly stats from journal.jsonl
	python review.py

review-claude:   ## weekly stats + a blunt written review
	python review.py --claude

kill:            ## stop the desk on its next cycle
	touch KILL
