.PHONY: setup seed test eval run down

setup:
	docker compose up -d --wait
	python data/seed.py

seed:
	python data/seed.py

test:
	python -m pytest -v

eval:
	python eval/run_eval.py

run:
	python src/slack/app.py

down:
	docker compose down -v
