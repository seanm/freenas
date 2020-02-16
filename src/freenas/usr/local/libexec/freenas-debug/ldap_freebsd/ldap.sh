#!/bin/sh
#+
# Copyright 2011 iXsystems, Inc.
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


ldap_opt() { echo l; }
ldap_help() { echo "Dump LDAP Configuration"; }
ldap_directory() { echo "LDAP"; }
ldap_func()
{
	local onoff

	onoff=$(${FREENAS_SQLITE_CMD} ${FREENAS_CONFIG} "
	SELECT
		ldap_enable
	FROM
		directoryservice_ldap

	ORDER BY
		-id

	LIMIT 1
	")

	enabled="DISABLED"
	if [ "${onoff}" = "1" ]
	then
		enabled="ENABLED"
	fi

	section_header "LDAP Status"
	echo "LDAP is ${enabled}"
	section_footer

	#
	#	dump LDAP configuration
	#
	local IFS="|"
	read hostname basedn binddn anonbind \
		cert validate_certs krb_realm krb_princ \
		ssl has_samba_schema <<-__LDAP__
	$(${FREENAS_SQLITE_CMD} ${FREENAS_CONFIG} "
	SELECT
		ldap_hostname,
		ldap_basedn,
		ldap_binddn,
		ldap_anonbind,
		ldap_certificate_id,
		ldap_validate_certificates,
		ldap_kerberos_realm_id,
		ldap_kerberos_principal,
		ldap_ssl,
		ldap_has_samba_schema

	FROM
		directoryservice_ldap

	ORDER BY
		-id

	LIMIT 1
	")
__LDAP__

	IFS="
"

	section_header "LDAP Settings"
	cat<<-__EOF__
	Hostname:               ${hostname}
	Base DN:                ${basedn}
	Bind DN:                ${binddn}
	Anonymous Bind:         ${anonbind}
	SSL:                    ${ssl}
	Cert:			${cert}
	Validate Certificates	${validate_certs}
	Kerberos realm		${krb_realm}
	Kerberos principal	${krb_princ}
	Samba Schema:           ${has_samba_schema}
__EOF__
	section_footer

	#
	#	Dump nsswitch.conf
	#
	section_header "${PATH_NS_CONF}"
	sc "${PATH_NS_CONF}"
	section_footer

	#
	#	Dump kerberos configuration
	#
	section_header "${PATH_KRB5_CONFIG}"
	sc "${PATH_KRB5_CONFIG}" 2>/dev/null
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
	#
	#	List kerberos tickets
	#
	section_header "Kerberos Tickets - 'klist'"
	klist
	section_footer

	#
	#	Dump LDAP configuration
	#
	section_header "${LDAP_CONF}"
	sc "${LDAP_CONF}"
	section_footer

	#
	#	Dump NSLCD configuration
	#
	section_header "${NSLCD_CONF}"
	sc "${NSLCD_CONF}" | grep -iv ldap_default_authtok
	section_footer

	#
	#	Dump generated LDAP config file
	#
	section_header "${LDAP_CONFIG_FILE}"
	sc "${LDAP_CONFIG_FILE}"
	section_footer

	#
	#	Dump nslcd state
	#
	if [ "${enabled}" = "ENABLED" ]
	then
	section_header "NSLCD health check - midclt call ldap.get_nslcd_status"
	midclt call ldap.get_nslcd_status | jq
	section_footer
	fi

}
