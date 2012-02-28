#!/bin/sh
     
# Parameter 1 is the application name
# Parameter 2 is the profile
# Parameter 3 is the URL
am start -a android.intent.action.VIEW -n $1 --es args "--profile $2" -d $3 