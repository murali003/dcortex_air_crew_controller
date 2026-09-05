#!/usr/bin/env bash
set -e
case "${1:-ui}" in
  doctor) python3 -m app.doctor ;;
  eval)   python3 -m eval.run_eval --engine ;;
  agent)  python3 -m eval.run_eval --agent ;;
  ask)    shift; python3 -m app.agent "$@" ;;
  ui|*)   streamlit run app/ui.py ;;
esac
