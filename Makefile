.PHONY: setup seed test eval run down

setup:
	docker compose up -d
	python data/seed.py

seed:
	python data/seed.py

test:
	python -m pytest -v

eval:
	python eval/run_eval.py

run:
	python -m src.slack.app

down:
	docker compose down -v
