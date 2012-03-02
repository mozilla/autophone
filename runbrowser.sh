#!/bin/sh

# Parameter 1 is the application name
# Parameter 2 is the URL
am start -a android.intent.action.VIEW -n $1 -d $2


