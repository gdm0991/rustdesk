Name:       dugadesk
Version:    1.4.9
Release:    0
Summary:    RPM package
License:    GPL-3.0
URL:        https://duga.pw
Vendor:     dugadesk <info@duga.pw>
Requires:   gtk3 libxcb1 libXfixes3 alsa-utils libXtst6 libva2 pam gstreamer-plugins-base gstreamer-plugin-pipewire
Recommends: libayatana-appindicator3-1 xdotool
Provides:   libdesktop_drop_plugin.so()(64bit), libdesktop_multi_window_plugin.so()(64bit), libfile_selector_linux_plugin.so()(64bit), libflutter_custom_cursor_plugin.so()(64bit), libflutter_linux_gtk.so()(64bit), libscreen_retriever_plugin.so()(64bit), libtray_manager_plugin.so()(64bit), liburl_launcher_linux_plugin.so()(64bit), libwindow_manager_plugin.so()(64bit), libwindow_size_plugin.so()(64bit), libtexture_rgba_renderer_plugin.so()(64bit)

# https://docs.fedoraproject.org/en-US/packaging-guidelines/Scriptlets/

%description
The best open-source remote desktop client software, written in Rust.

%prep
# we have no source, so nothing here

%build
# we have no source, so nothing here

# %global __python %{__python3}

%install

mkdir -p "%{buildroot}/usr/share/dugadesk" && cp -r ${HBB}/flutter/build/linux/x64/release/bundle/* -t "%{buildroot}/usr/share/dugadesk"
mkdir -p "%{buildroot}/usr/bin"
install -Dm 644 $HBB/res/dugadesk.service -t "%{buildroot}/usr/share/dugadesk/files"
install -Dm 644 $HBB/res/dugadesk.desktop -t "%{buildroot}/usr/share/dugadesk/files"
install -Dm 644 $HBB/res/dugadesk-link.desktop -t "%{buildroot}/usr/share/dugadesk/files"
install -Dm 644 $HBB/res/128x128@2x.png "%{buildroot}/usr/share/icons/hicolor/256x256/apps/dugadesk.png"
install -Dm 644 $HBB/res/scalable.svg "%{buildroot}/usr/share/icons/hicolor/scalable/apps/dugadesk.svg"

%files
/usr/share/dugadesk/*
/usr/share/dugadesk/files/dugadesk.service
/usr/share/icons/hicolor/256x256/apps/dugadesk.png
/usr/share/icons/hicolor/scalable/apps/dugadesk.svg
/usr/share/dugadesk/files/dugadesk.desktop
/usr/share/dugadesk/files/dugadesk-link.desktop

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
ln -sf /usr/share/dugadesk/dugadesk /usr/bin/dugadesk
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
    rm /usr/bin/dugadesk || true
    rmdir /usr/lib/dugadesk || true
    rmdir /usr/local/dugadesk || true
    rmdir /usr/share/dugadesk || true
    rm /usr/share/applications/dugadesk.desktop || true
    rm /usr/share/applications/dugadesk-link.desktop || true
    update-desktop-database
  ;;
  1)
    # for upgrade
    rmdir /usr/lib/dugadesk || true
    rmdir /usr/local/dugadesk || true
  ;;
esac
