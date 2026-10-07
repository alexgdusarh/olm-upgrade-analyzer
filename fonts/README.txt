Red Hat Display, Red Hat Text and Red Hat Mono (variable-weight woff2), the
same files as ocp_preupgrade_health_check/templates/fonts/. The report copies
them once into each cluster's html/fonts/, and every page loads them from
there, so the reports keep their PatternFly look offline (attached to a
ticket, opened on a disconnected bastion). Taken from @patternfly/patternfly
6.6.1 (assets/fonts/). Licensed under the SIL Open Font License 1.1: OFL.txt.
If a file is missing, the pages fall back to system fonts.
