# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

finradar is a DART-based early-warning system for corporate financial risk in Korean listed companies. DART (Data Analysis, Retrieval and Transfer System) is the Financial Supervisory Service's corporate disclosure database — expect integration with the DART OpenAPI, which requires an API key.

The project is at an early, pre-code stage: no source files, dependencies, or tooling exist yet. This file is a living document — update it as the stack, structure, and conventions are established.

## Stack

- Python, intended as a data pipeline / scheduled job (pulls DART filings, computes risk signals, alerts) rather than a long-running API service.
- Dependencies managed via `pip` + `requirements.txt` (not yet created).
