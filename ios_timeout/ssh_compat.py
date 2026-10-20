"""SSH compatibility for the selected USB ramdisk, without global changes."""


def usb_transport(sock):
    import paramiko
    from cryptography.hazmat.primitives import hashes

    # OpenSSH 5.2 in the ARMv6 ramdisk only signs its RSA host key with SHA-1.
    # Paramiko 5 removed that algorithm. Keep modern algorithms preferred and
    # add only this host-key signature to this USB transport. Password auth,
    # host-key pinning, ciphers and key exchange retain their existing policy.
    class RamdiskRSAKey(paramiko.RSAKey):
        HASHES = {**paramiko.RSAKey.HASHES, "ssh-rsa": hashes.SHA1}

    transport = paramiko.Transport(sock)
    transport._key_info = {**transport._key_info, "ssh-rsa": RamdiskRSAKey}
    options = transport.get_security_options()
    if "ssh-rsa" not in options.key_types:
        options.key_types = (*options.key_types, "ssh-rsa")
    return transport
