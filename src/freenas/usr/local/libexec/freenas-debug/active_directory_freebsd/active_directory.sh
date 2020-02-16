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


active_directory_opt() { echo a; }
active_directory_help() { echo "Dump Active Directory Configuration"; }
active_directory_directory() { echo "ActiveDirectory"; }
active_directory_func()
{
	local workgroup
	local netbiosname
	local adminname
	local domainname
	local dcname
	local pamfiles
	local onoff
	local enabled="DISABLED"
	local cifs_onoff


	#
	#	Second, check if the Active Directory is enabled.
	#
	onoff=$(${FREENAS_SQLITE_CMD} ${FREENAS_CONFIG} "
	SELECT
		ad_enable
	FROM
		directoryservice_activedirectory
	ORDER BY
		-id

	LIMIT 1
	")

	enabled="DISABLED"
	if [ "${onoff}" = "1" ]
	then
		enabled="ENABLED"
	fi
	
	section_header "Active Directory Status"
	echo "Active Directory is ${enabled}"
	section_footer

	section_header "Active Directory Run Status"
	service samba_server onestatus
	section_header

	#
	#	Check if SMB service is set to start on boot.
	#
	cifs_onoff=$(${FREENAS_SQLITE_CMD} ${FREENAS_CONFIG} "
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

	cifs_enabled="not start on boot."
	if [ "$cifs_onoff" == "1" ]
	then
		cifs_enabled="start on boot."
	fi

	section_header "SMB Service Status"
	echo "SMB will $cifs_enabled"
	section_footer

	#
	#	Next, dump Active Directory configuration
	#
	local IFS="|"
	read domainname bindname ssl allow_trusted_doms use_default_domain \
		validate_certs cert krb_realm krb_princ create_computer \
		sasl_wrapping timeout dns_timeout <<-__AD__
	$(${FREENAS_SQLITE_CMD} ${FREENAS_CONFIG} "
	SELECT
		ad_domainname,
		ad_bindname,
		ad_ssl,
		ad_allow_trusted_doms,
		ad_use_default_domain,
		ad_validate_certificates,
		ad_certificate_id,
		ad_kerberos_realm_id,
		ad_kerberos_principal,
		ad_createcomputer,
		ad_ldap_sasl_wrapping,
		ad_timeout,
		ad_dns_timeout

	FROM
		directoryservice_activedirectory

	ORDER BY
		-id

	LIMIT 1
	")
__AD__

	IFS="
"

	section_header "Active Directory Settings"
	cat<<-__EOF__
	Domain:                 ${domainname}
	Bind name:              ${bindname}
	Trusted domains:        ${allow_trusted_doms}
	SSL:                    ${ssl}
	Cert:                   ${cert}
	Validate_certs:         ${validate_certs}
	Kerberos_realm:         ${krb_realm}
	Kerberos_principal:     ${krb_princ}
	Default_computer_OU:    ${create_computer}
	LDAP_SASL_Wrapping:     ${sasl_wrapping}
	Timeout:                ${timeout}
	DNS Timeout:            ${dns_timeout}
__EOF__
	section_footer

	#
	#	Dump kerberos configuration
	#
	section_header "${PATH_KRB5_CONFIG}"
	sc "${PATH_KRB5_CONFIG}" 2>/dev/null
	section_footer

	#
	#	Dump nsswitch.conf
	#
	section_header "${PATH_NS_CONF}"
	sc "${PATH_NS_CONF}"
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
	#	List kerberos keytab entries
	#
	section_header "Kerberos Principals - 'ktutil'"
	ktutil list
	section_footer

	#
	#	Dump Active Directory Domain Information
	#
	if [ "${enabled}" = "ENABLED" ]
	then
	section_header "Active Directory Domain Info - 'midclt call activedirectory.domain_info'"
	midclt call activedirectory.domain_info | jq
	section_footer
	fi

	#
	#	Dump wbinfo information
	#
	section_header "Active Directory Trust Secret - 'wbinfo -t'"
	wbinfo -t
	section_footer
	section_header "Active Directory NETLOGON connection - 'wbinfo -P'"
	wbinfo -P
	section_footer
	section_header "Active Directory trusted domains - 'wbinfo -m'"
	wbinfo -m
	section_footer
	section_header "Active Directory all domains - 'wbinfo --all-domains'"
	wbinfo --all-domains
	section_footer
	section_header "Active Directory own domain - 'wbinfo --own-domain'"
	wbinfo --own-domain
	section_footer
	section_header "Active Directory online status - 'wbinfo --online-status'"
	wbinfo --online-status
	section_footer
	section_header "Active Directory domain info - 'wbinfo --domain-info=$(wbinfo --own-domain)'"
	wbinfo --domain-info="$(wbinfo --own-domain)"
	section_footer
	section_header "Active Directory DC name - 'wbinfo --dsgetdcname=${domainname}'"
	wbinfo --dsgetdcname="${domainname}"
	section_footer
	section_header "Active Directory DC info - 'wbinfo --dc-info=$(wbinfo --own-domain)'"
	wbinfo --dc-info="$(wbinfo --own-domain)"
	section_footer

	#
	#	Dump Active Directory users and groups
	#
	section_header "Active Directory Users - 'wbinfo -u'"
	wbinfo -u | head -50
	section_header "Active Directory Groups - 'wbinfo -g'"
	wbinfo -g | head -50
	section_footer

	#
	#	Dump results of testjoin
	#
	if [ "${enabled}" = "ENABLED" ]
	then
	section_header "Active Directory Join Status net -d 5 -k ads testjoin"
	net -d 5 -k ads testjoin
	section_footer
	fi

	#
	#	Dump results clockskew check
	#
	if [ "${enabled}" = "ENABLED" ]
	then
	section_header "Active Directory clockskew - midclt call activedirectory.check_clockskew"
	midclt call activedirectory.check_clockskew | jq
	section_footer
	fi
}
