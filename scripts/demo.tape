# Renders the crash-durability demo to demo.gif.
#
# Prereqs:
#   brew install vhs
#   a running Postgres, with HAPAX_DATABASE_URL set (or edit the command below)
#
# Then:  vhs scripts/demo.tape   ->   produces demo.gif

Output demo.gif

Set FontSize 15
Set Width 1100
Set Height 900
Set Theme "Dracula"
Set Padding 20

Type "clear && .venv/bin/python scripts/demo.py"
Enter
Sleep 8s
