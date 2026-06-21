Name:           screen-autorotate
Version:        1.0.0
Release:        1%{?dist}
Summary:        Automatic screen rotation for ASUS ProArt PX13 on GNOME Wayland

License:        GPL-3.0-or-later
URL:            https://github.com/devcoons/screen-autorotate
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  systemd-rpm-macros

Requires:       iio-sensor-proxy
Requires:       python3-gobject
Requires:       systemd
Requires:       udev

%description
System service that auto-rotates the internal panel (eDP-1) on the ASUS
ProArt PX13 (HN7306) using the AMD HID accelerometer via iio-sensor-proxy
and GNOME Mutter DisplayConfig. Works on the GDM login screen and in user
sessions on Fedora/GNOME Wayland.

%prep
%autosetup -n %{name}-%{version}

%build
# noarch: nothing to compile

%install
install -d %{buildroot}%{_libexecdir}/screen-autorotate
install -d %{buildroot}%{_unitdir}/iio-sensor-proxy.service.d
install -d %{buildroot}%{_docdir}/%{name}

install -pm 0755 lib/autorotate.py %{buildroot}%{_libexecdir}/screen-autorotate/
install -pm 0755 bin/screen-autorotatectl %{buildroot}%{_bindir}/
install -pm 0644 config/screen-autorotate.conf %{buildroot}%{_sysconfdir}/screen-autorotate.conf
install -pm 0644 systemd/screen-autorotate.service %{buildroot}%{_unitdir}/
install -pm 0644 udev/61-proart-px13-accel.rules %{buildroot}%{_udevrulesdir}/
install -pm 0644 systemd/iio-sensor-proxy.service.d/override.conf \
    %{buildroot}%{_unitdir}/iio-sensor-proxy.service.d/
install -pm 0644 README.md %{buildroot}%{_docdir}/%{name}/

%post
%systemd_post screen-autorotate.service
udevadm control --reload-rules || :
udevadm trigger --subsystem-match=iio || :

%preun
%systemd_preun screen-autorotate.service

%postun
%systemd_postun_with_restart screen-autorotate.service
udevadm control --reload-rules || :

%files
%doc %{_docdir}/%{name}/README.md
%{_libexecdir}/screen-autorotate/autorotate.py
%{_bindir}/screen-autorotatectl
%config(noreplace) %{_sysconfdir}/screen-autorotate.conf
%{_unitdir}/screen-autorotate.service
%{_udevrulesdir}/61-proart-px13-accel.rules
%{_unitdir}/iio-sensor-proxy.service.d/override.conf

%changelog
* Sat Jun 21 2026 devcoons <devcoons@users.noreply.github.com> - 1.0.0-1
- Initial package for ASUS ProArt PX13
