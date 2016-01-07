# Appendix

## Authenticate by Security File

VNX credentials are necessary when the driver connects to the VNX system.
Credentials in global, local and ldap scopes are supported. There are two
approaches to provide the credentials:

The recommended one is using the Navisphere CLI security file to provide the
credentials which can get rid of providing the plain text credentials in the
configuration file. Following is the instruction on how to do this.

1. Find out the Linux user id of the `/usr/bin/cinder-volume` processes.
   Assuming the service /usr/bin/cinder-volume is running by the account `cinder`

2. Switch to root account with `sudo su`

3. In `/etc/passwd`, change `cinder:x:113:120::/var/lib/cinder:/bin/false`
   to `cinder:x:113:120::/var/lib/cinder:/bin/bash`
   (This temporary change is to make step 4 work.)

4. Save the credentials on behave of `cinder` user to a security file(assuming
   the array credentials are `admin/admin` in `global` scope). In the command
   below, the '-secfilepath' switch is used to specify the location to save the
   security file.

        $ su -l cinder -c '/opt/Navisphere/bin/naviseccli -AddUserSecurity -user admin -password admin -scope 0 -secfilepath <location>'

5. Change `cinder:x:113:120::/var/lib/cinder:/bin/bash` back to
   `cinder:x:113:120::/var/lib/cinder:/bin/false` in `/etc/passwd`

6. Remove the credentials options `san_login`, `san_password` and
   `storage_vnx_authentication_type` from cinder.conf.
   (normally it is `/etc/cinder/cinder.conf`).
   Add option `storage_vnx_security_file_dir` and set its value to the directory
   path of your security file generated in step 4. Omit this option if
   `-secfilepath` is not used in step 4.

7. Restart the cinder-volume service to validate the change.

## Register FC port with VNX

__Note:__

This configuration is only required when `initiator_auto_registration=False`.

To access VNX storage, the compute nodes should be registered on VNX first if
initiator auto registration is not enabled.

To perform "Copy Image to Volume" and "Copy Volume to Image" operations, the
nodes running the cinder-volume service (Block Storage nodes) must be registered
with the VNX as well.

The steps mentioned below are for the compute nodes. Please follow the same
steps for the Block Storage nodes also (The steps can be skipped if initiator
auto registration is enabled).

* Assume `20:00:00:24:FF:48:BA:C2:21:00:00:24:FF:48:BA:C2` is WWN of the FC
  initiator port name of the compute node. Register
  `20:00:00:24:FF:48:BA:C2:21:00:00:24:FF:48:BA:C2` in Unisphere
    * Login Unisphere, go to `FNM0000000000->Hosts->Initiators`,
    * Refresh and wait until initiator
      `20:00:00:24:FF:48:BA:C2:21:00:00:24:FF:48:BA:C2` with SP Port
      `A-1` appears.
    * Click the `Register` button, select `CLARiiON/VNX` and enter the host
      name (which is the output of the linux command `hostname`) and IP address:
        * Hostname : myhost1
        * IP : 10.10.61.1
        * Click Register.
    * Now host 10.10.61.1 will appear under Hosts->Host List as well.
* Register the WWN with more ports if needed.

## Register iSCSI port with VNX

__Note:__

This configuration is only required when `initiator_auto_registration=False`.

To access VNX storage, the compute nodes should be registered on VNX first if
initiator auto registration is not enabled.

To perform "Copy Image to Volume" and "Copy Volume to Image" operations, the
nodes running the cinder-volume service (Block Storage nodes) must be registered
with the VNX as well.

The steps mentioned below are for the compute nodes. Please follow the same
steps for the Block Storage nodes also (The steps can be skipped if initiator
auto registration is enabled).

* On the node with IP address 10.10.61.1, do the following steps
(assuming 10.10.61.35 is the iSCSI target):

        # Start the iSCSI initiator service on the node
        $ sudo /etc/init.d/open-iscsi start
        # Discover the iSCSI target portals on VNX
        $ sudo iscsiadm -m discovery -t st -p 10.10.61.35
        $ cd /etc/iscsi
        # Find out the IQN of the node
        $ sudo more initiatorname.iscsi

* Login VNX from the compute node using the target corresponding to the SPA port:

        $ sudo iscsiadm -m node -T iqn.1992-04.com.emc:cx.apm01234567890.a0 -p 10.10.61.35 -l

* Assume `iqn.1993-08.org.debian:01:1a2b3c4d5f6g` is the initiator name of the
compute node. Register `iqn.1993-08.org.debian:01:1a2b3c4d5f6g` in Unisphere

    * Login Unisphere, go to `FNM0000000000->Hosts->Initiators`,
    * Refresh and wait until the initiator `iqn.1993-08.org.debian:01:1a2b3c4d5f6g`
      with SP Port `A-8v0` appears.
    * Click the `Register` button, select `CLARiiON/VNX` and enter the host name
      (which is the output of the linux command `hostname`) and IP address:
        * Hostname : myhost1
        * IP : 10.10.61.1
        * Click Register.
    * Now the host 10.10.61.1 will appear under Hosts->Host List as well.

* Logout iSCSI on the node:

        $ sudo iscsiadm -m node -u

* Login VNX from the compute node using the target corresponding to the SPB port:

        $ sudo iscsiadm -m node -T iqn.1992-04.com.emc:cx.apm01234567890.b8 -p 10.10.61.36 -l

* Register the initiator with the SPB port in Unisphere .

* Logout iSCSI on the node:

        $ sudo iscsiadm -m node -u

* Register the IQN with more ports if needed.

