#!/bin/sh
#+
# Copyright 2015 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################


smb_opt() { echo C; }
smb_help() { echo "Dump SMB Configuration"; }
smb_directory() { echo "SMB"; }
smb_func()
{
	local workgroup
	local netbiosname
	local adminname
	local domainname
	local dcname
	local pamfiles
	local onoff


	onoff=$(${FREENAS_SQLITE_CMD} ${FREENAS_CONFIG} "
	SELECT
		srv_enable
	FROM
		services_services
	WHERE
		srv_service = 'cifs'
	ORDER BY
		-id
	LIMIT 1
	")

	enabled="not start on boot."
	if [ "${onoff}" = "1" ]
	then
		enabled="start on boot."
	fi

	section_header "SMB Boot Status"
	echo "SMB will ${enabled}"
	section_footer

	#
	#	Dump samba version
	#
	section_header "smbd -V"
	smbd -V
	section_footer


	#
	#	Dump samba configuration
	#
	section_header "${SAMBA_CONF}"
	sc "${SAMBA_CONF}"
	section_footer

	section_header "${SAMBA_SHARE_CONF}"
	sc "${SAMBA_SHARE_CONF}"
	section_footer

	local IFS="|"

	#
	#	Dump SMB shares
	#
	section_header "SMB Shares & Permissions"
	${FREENAS_SQLITE_CMD} ${FREENAS_CONFIG} "
	SELECT
		cifs_path,
		cifs_name
	FROM
		sharing_cifs_share
	ORDER BY
		-id
	" | while read -r cifs_path cifs_name
	do
		section_header "${cifs_name}:${cifs_path}"
		ls -ld "${cifs_path}"
		printf "\n"
		getfacl "${cifs_path}"
		printf "\n"
	done
	section_footer

	#
	#	Dump samba build options
	#
	section_header "smbd -b"
	smbd -b
	section_footer

	section_header "net getlocalsid"
	net getlocalsid
	section_footer
	section_header "net getdomainsid"
	net getdomainsid
	section_footer
	section_header "net groupmap list"
	net groupmap list | head -50
	section_footer

	section_header "net status sessions"
	net status sessions | head -50
	section_footer
	section_header "net status shares"
	net status shares
	section_footer

	section_header "Lock information"
	smbstatus -L | head -50
	section_footer
	
	section_header "ACLs - 'sharesec --view-all'"
	sharesec --view-all
	section_footer

	section_header "Local users in passdb.tdb"
	pdbedit -Lv
	section_footer
}
