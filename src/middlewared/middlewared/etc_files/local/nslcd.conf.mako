#
# NSLCD.CONF(5)		The configuration file for LDAP nameservice daemon
# $FreeBSD$
#
<%
        ldap = middleware.call_sync('ldap.config')
        if ldap:
            certpath = None
            if ldap['certificate']:
                try:
                    cert = middleware.call_sync('certificate.query', [('id', '=', ldap['certificate'])], {'get': True})
                except IndexError:
                    pass
                else:
                    certpath = cert['certificate_path']
                    keypath = cert['privatekey_path']
        else:
            ldap = None

        ldap_enabled = ldap['enable']
%>
% if ldap_enabled:
    uri 	${' '.join(ldap['uri_list'])}
    base 	${ldap['basedn']}
  % if ldap['ssl'] in ('START_TLS', 'ON'):
    ssl 	${ldap['ssl'].lower()}
    tls_cacert	/etc/ssl/truenas_cacerts.pem
    % if certpath:
    tls_cert	${certpath}
    tls_key	${keypath}
    sasl_mech	EXTERNAL
    % endif
    tls_reqcert ${'demand' if ldap['validate_certificates'] else 'allow'}
  % endif
  % if ldap['binddn'] and ldap['bindpw']:
    binddn 	${ldap['binddn']}
    bindpw 	${ldap['bindpw']}
  % endif
  % if ldap['disable_freenas_cache']:
    nss_disable_enumeration yes
  % endif
  % if ldap['kerberos_realm']:
    sasl_mech 	GSSAPI
    sasl_realm	${ldap['kerberos_realm']}
  % endif
    scope 	sub
    timelimit	${ldap['timeout']}
    bind_timelimit ${ldap['dns_timeout']}
    map passwd loginShell /bin/sh
  % if ldap['auxiliary_parameters']:
    ${ldap['auxiliary_parameters']}
  % endif
% endif
