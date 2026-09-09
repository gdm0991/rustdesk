Name:       dugadesk
Version:    1.4.9
Release:    0
Summary:    RPM package
License:    GPL-3.0
URL:        https://duga.pw
Vendor:     dugadesk <info@duga.pw>
Requires:   gtk3 libxcb libXfixes alsa-lib libva2 pam gstreamer1-plugins-base
Recommends: libayatana-appindicator-gtk3 libxdo

# https://docs.fedoraproject.org/en-US/packaging-guidelines/Scriptlets/

%description
The best open-source remote desktop client software, written in Rust.

%prep
# we have no source, so nothing here

%build
# we have no source, so nothing here

%global __python %{__python3}

%install
mkdir -p %{buildroot}/usr/bin/
mkdir -p %{buildroot}/usr/share/dugadesk/
mkdir -p %{buildroot}/usr/share/dugadesk/files/
mkdir -p %{buildroot}/usr/share/icons/hicolor/256x256/apps/
mkdir -p %{buildroot}/usr/share/icons/hicolor/scalable/apps/
install -m 755 $HBB/target/release/dugadesk %{buildroot}/usr/bin/dugadesk
install $HBB/libsciter-gtk.so %{buildroot}/usr/share/dugadesk/libsciter-gtk.so
install $HBB/res/dugadesk.service %{buildroot}/usr/share/dugadesk/files/
install $HBB/res/128x128@2x.png %{buildroot}/usr/share/icons/hicolor/256x256/apps/dugadesk.png
install $HBB/res/scalable.svg %{buildroot}/usr/share/icons/hicolor/scalable/apps/dugadesk.svg
install $HBB/res/dugadesk.desktop %{buildroot}/usr/share/dugadesk/files/
install $HBB/res/dugadesk-link.desktop %{buildroot}/usr/share/dugadesk/files/

%files
/usr/bin/dugadesk
/usr/share/dugadesk/libsciter-gtk.so
/usr/share/dugadesk/files/dugadesk.service
/usr/share/icons/hicolor/256x256/apps/dugadesk.png
/usr/share/icons/hicolor/scalable/apps/dugadesk.svg
/usr/share/dugadesk/files/dugadesk.desktop
/usr/share/dugadesk/files/dugadesk-link.desktop
/usr/share/dugadesk/files/__pycache__/*

%changelog
# let's skip this for now

%pre
# can do something for centos7
case "$1" in
  1)
    # for install
  ;;
  2)
    # for upgrade
    systemctl stop dugadesk || true
  ;;
esac

%post
cp /usr/share/dugadesk/files/dugadesk.service /etc/systemd/system/dugadesk.service
cp /usr/share/dugadesk/files/dugadesk.desktop /usr/share/applications/
cp /usr/share/dugadesk/files/dugadesk-link.desktop /usr/share/applications/
systemctl daemon-reload
systemctl enable dugadesk
systemctl start dugadesk
update-desktop-database

%preun
case "$1" in
  0)
    # for uninstall
    systemctl stop dugadesk || true
    systemctl disable dugadesk || true
    rm /etc/systemd/system/dugadesk.service || true
  ;;
  1)
    # for upgrade
  ;;
esac

%postun
case "$1" in
  0)
    # for uninstall
    rm /usr/share/applications/dugadesk.desktop || true
    rm /usr/share/applications/dugadesk-link.desktop || true
    update-desktop-database
  ;;
  1)
    # for upgrade
  ;;
esac
