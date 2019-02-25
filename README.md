[![Build Status](https://travis-ci.org/asottile/dead.svg?branch=master)](https://travis-ci.org/asottile/dead)

dead
====

dead simple python dead code detection

## installation

`pip install dead`


## cli

Consult the help for the latest usage:

```console
$ dead --help
usage: dead [-h] [--files FILES] [--exclude EXCLUDE] [--tests TESTS]

optional arguments:
  -h, --help         show this help message and exit
  --files FILES      regex for file inclusion, default: ''
  --exclude EXCLUDE  regex for file exclusion, default '^$'
  --tests TESTS      regex to mark files as tests, default
                     '(^|/)(tests?|testing)/'
```

run the `dead` utility from the root of a git repository.

## as a pre-commit hook

See [pre-commit](https://github.com/pre-commit/pre-commit) for instructions

Sample `.pre-commit-config.yaml`:

```yaml
-   repo: https://github.com/asottile/dead
    rev: v0.0.5
    hooks:
    -   id: dead
```

### how it works

1. find all files in a repository using `git ls-files` and filtering:
    - only include files matched by the `--files` regex
    - exclude files matched by the `--exclude` regex
    - only include files identified as `python` by
      [`identify`](https://github.com/chriskuehl/identify)
    - classify test files by the `--tests` regex
1. ast parse each file
    - search for definitions and references
1. report things which do not have references

### false positives

I wrote this in ~15 minutes on an airplane, it's far from perfect but often
finds things.  Here's a few things it's not good at:

- functions which implement an interface are often marked as unused
- metaclass magic is often marked as unused (enums, model classes, etc.)

### suppressing `dead`

The `# dead: disable` comment will tell `dead` to ignore
any line which has reportedly dead code.

### is this project dead?

_maybe._
