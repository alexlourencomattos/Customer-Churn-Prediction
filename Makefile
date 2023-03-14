SOURCES = $(wildcard *.py) $(wildcard */*.py) $(wildcard */*/*.py)

train:
	poetry run train

run-mlflow-server:
	poetry run mlflow server --host 0.0.0.0 --port 5000

all-tests: black mypy tests

tests:
	poetry run nox -rs tests

black:
	poetry run nox -rs black

mypy:
	poetry run nox -rs mypy

tags:
	ctags -f tags -R --fields=+iaS --extra=+q $(SOURCES)

include-tags:
	ctags -f include_tags -R --languages=python --fields=+iaS --extra=+q \
		.venv/lib/python3.9/

sync-with-git:
	git fetch
	git reset origin/main --hard

clean:
	rm -rf tags include_tags __pycache__ */__pycache__ */*/__pycache__

.PHONY: clean include-tags tags tests

