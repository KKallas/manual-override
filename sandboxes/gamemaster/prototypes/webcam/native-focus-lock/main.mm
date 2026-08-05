#import <AVFoundation/AVFoundation.h>
#import <CoreFoundation/CoreFoundation.h>
#import <IOKit/IOCFPlugIn.h>
#import <IOKit/IOKitLib.h>
#import <IOKit/usb/IOUSBLib.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <algorithm>

namespace {
constexpr UInt8 kFocusAbsolute = 0x06;
constexpr UInt8 kFocusAuto = 0x08;
constexpr UInt8 kExposureMode = 0x02;
constexpr UInt8 kExposureAbsolute = 0x04;

struct FocusController {
  IOUSBInterfaceInterface190 **interface = nullptr;
  UInt8 terminal = 0;
  UInt8 interface_number = 0;
  bool auto_supported = false;
  bool absolute_supported = false;
  bool autofocus = true;
  int minimum = 0, maximum = 255, step = 1, default_value = 0, current = 0;
  bool autoexposure_supported = false;
  bool exposure_supported = false;
  bool autoexposure = true;
  int exposure_minimum = 0, exposure_maximum = 0, exposure_step = 1;
  int exposure_default = 0, exposure_current = 0;

  ~FocusController() {
    if (interface) {
      (*interface)->Release(interface);
    }
  }

  bool read(UInt8 selector, UInt8 request_code, UInt16 length, int &output) {
    UInt32 value = 0;
    IOUSBDevRequest request{
        0xA1, request_code, static_cast<UInt16>(selector << 8),
        static_cast<UInt16>((terminal << 8) | interface_number), length,
        &value, 0};
    IOReturn result = (*interface)->ControlRequest(interface, 0, &request);
    if (result != kIOReturnSuccess) {
      return false;
    }
    value = OSSwapLittleToHostInt32(value);
    UInt32 mask = length == 1 ? 0xff : (length == 2 ? 0xffff : 0xffffffff);
    output = static_cast<int>(value & mask);
    return true;
  }

  bool write(UInt8 selector, int value, UInt16 length) {
    UInt32 raw = OSSwapHostToLittleInt32(static_cast<UInt32>(value));
    IOUSBDevRequest request{
        0x21, 0x01, static_cast<UInt16>(selector << 8),
        static_cast<UInt16>((terminal << 8) | interface_number), length,
        &raw, 0};
    return (*interface)->ControlRequest(interface, 0, &request) ==
           kIOReturnSuccess;
  }

  void refresh() {
    int info = 0;
    auto_supported = read(kFocusAuto, 0x86, 1, info) && (info & 0x03);
    absolute_supported =
        read(kFocusAbsolute, 0x86, 1, info) && (info & 0x03);
    if (auto_supported) {
      int value = 1;
      read(kFocusAuto, 0x81, 1, value);
      autofocus = value != 0;
    }
    if (absolute_supported) {
      absolute_supported =
          read(kFocusAbsolute, 0x82, 2, minimum) &&
          read(kFocusAbsolute, 0x83, 2, maximum) &&
          read(kFocusAbsolute, 0x84, 2, step) &&
          read(kFocusAbsolute, 0x87, 2, default_value) &&
          read(kFocusAbsolute, 0x81, 2, current) && maximum > minimum;
      if (step < 1) step = 1;
    }
    autoexposure_supported =
        read(kExposureMode, 0x86, 1, info) && (info & 0x03);
    int exposure_mode = 8;
    if (autoexposure_supported) {
      read(kExposureMode, 0x81, 1, exposure_mode);
      autoexposure = exposure_mode != 1;
    }
    exposure_supported =
        read(kExposureAbsolute, 0x86, 1, info) && (info & 0x03);
    if (exposure_supported) {
      exposure_supported =
          read(kExposureAbsolute, 0x82, 4, exposure_minimum) &&
          read(kExposureAbsolute, 0x83, 4, exposure_maximum) &&
          read(kExposureAbsolute, 0x84, 4, exposure_step) &&
          read(kExposureAbsolute, 0x87, 4, exposure_default) &&
          read(kExposureAbsolute, 0x81, 4, exposure_current) &&
          exposure_maximum > exposure_minimum;
      if (exposure_step < 1) exposure_step = 1;
    }
  }
};

void releasePlugin(IOCFPlugInInterface **plugin) {
  if (plugin) (*plugin)->Release(plugin);
}

bool queryInterface(IOCFPlugInInterface **plugin, CFUUIDRef uuid, void **output) {
  return (*plugin)->QueryInterface(
             plugin, CFUUIDGetUUIDBytes(uuid), output) == kIOReturnSuccess &&
         *output;
}

bool parseDescriptors(const IOUSBConfigurationDescriptor *config,
                      UInt8 &terminal, UInt8 &interface_number) {
  int total = OSSwapLittleToHostInt16(config->wTotalLength);
  const UInt8 *bytes = reinterpret_cast<const UInt8 *>(config);
  int offset = 0;
  UInt8 current_interface = 0;
  bool video_control = false;
  while (offset + 3 <= total) {
    int length = bytes[offset];
    if (length < 3 || offset + length > total) break;
    UInt8 type = bytes[offset + 1];
    if (type == kUSBInterfaceDesc && length >= 9) {
      current_interface = bytes[offset + 2];
      video_control =
          bytes[offset + 5] == 0x0e && bytes[offset + 6] == 0x01;
    } else if (video_control && type == 0x24 &&
               bytes[offset + 2] == 0x02 && length >= 8) {
      UInt16 terminal_type =
          bytes[offset + 4] | static_cast<UInt16>(bytes[offset + 5] << 8);
      if (terminal_type == 0x0201) {
        terminal = bytes[offset + 3];
        interface_number = current_interface;
        return true;
      }
    }
    offset += length;
  }
  return false;
}

bool openController(int vendor, int product, FocusController &controller,
                    char *error, size_t error_size) {
  CFMutableDictionaryRef matching = IOServiceMatching("IOUSBDevice");
  CFNumberRef vendor_value =
      CFNumberCreate(nullptr, kCFNumberIntType, &vendor);
  CFNumberRef product_value =
      CFNumberCreate(nullptr, kCFNumberIntType, &product);
  CFDictionarySetValue(matching, CFSTR("idVendor"), vendor_value);
  CFDictionarySetValue(matching, CFSTR("idProduct"), product_value);
  CFRelease(vendor_value);
  CFRelease(product_value);

  io_iterator_t devices = IO_OBJECT_NULL;
  if (IOServiceGetMatchingServices(kIOMainPortDefault, matching, &devices) !=
      kIOReturnSuccess) {
    std::snprintf(error, error_size, "USB device lookup failed");
    return false;
  }
  io_service_t service = IOIteratorNext(devices);
  IOObjectRelease(devices);
  if (!service) {
    std::snprintf(error, error_size, "matching USB camera was not found");
    return false;
  }

  IOCFPlugInInterface **device_plugin = nullptr;
  SInt32 score = 0;
  IOReturn result = IOCreatePlugInInterfaceForService(
      service, kIOUSBDeviceUserClientTypeID, kIOCFPlugInInterfaceID,
      &device_plugin, &score);
  IOObjectRelease(service);
  if (result != kIOReturnSuccess || !device_plugin) {
    std::snprintf(error, error_size, "could not create USB device plugin");
    return false;
  }
  IOUSBDeviceInterface **device = nullptr;
  if (!queryInterface(device_plugin, kIOUSBDeviceInterfaceID,
                      reinterpret_cast<void **>(&device))) {
    releasePlugin(device_plugin);
    std::snprintf(error, error_size, "could not resolve USB device interface");
    return false;
  }
  releasePlugin(device_plugin);

  IOUSBConfigurationDescriptorPtr config = nullptr;
  bool descriptors_ok =
      (*device)->GetConfigurationDescriptorPtr(device, 0, &config) ==
          kIOReturnSuccess &&
      config && parseDescriptors(config, controller.terminal,
                                 controller.interface_number);
  if (!descriptors_ok) {
    (*device)->Release(device);
    std::snprintf(error, error_size,
                  "camera-terminal descriptor was not found");
    return false;
  }

  IOUSBFindInterfaceRequest find{
      0x0e, 0x01, kIOUSBFindInterfaceDontCare, kIOUSBFindInterfaceDontCare};
  io_iterator_t interfaces = IO_OBJECT_NULL;
  result = (*device)->CreateInterfaceIterator(device, &find, &interfaces);
  (*device)->Release(device);
  if (result != kIOReturnSuccess) {
    std::snprintf(error, error_size,
                  "could not enumerate UVC control interface");
    return false;
  }
  io_service_t interface_service = IOIteratorNext(interfaces);
  IOObjectRelease(interfaces);
  if (!interface_service) {
    std::snprintf(error, error_size, "UVC control interface was not found");
    return false;
  }
  IOCFPlugInInterface **interface_plugin = nullptr;
  result = IOCreatePlugInInterfaceForService(
      interface_service, kIOUSBInterfaceUserClientTypeID,
      kIOCFPlugInInterfaceID, &interface_plugin, &score);
  IOObjectRelease(interface_service);
  if (result != kIOReturnSuccess || !interface_plugin) {
    std::snprintf(error, error_size,
                  "could not create UVC control-interface plugin");
    return false;
  }
  bool queried = queryInterface(
      interface_plugin, kIOUSBInterfaceInterfaceID190,
      reinterpret_cast<void **>(&controller.interface));
  releasePlugin(interface_plugin);
  if (!queried) {
    std::snprintf(error, error_size,
                  "could not resolve UVC control interface");
    return false;
  }
  // Deliberately do not call USBInterfaceOpen. Apple's UVC driver owns the
  // interface, but class-specific ControlRequest calls can coexist with it.
  controller.refresh();
  return true;
}

void printError(const char *message) {
  NSString *text = [NSString stringWithUTF8String:message];
  NSData *json = [NSJSONSerialization
      dataWithJSONObject:@{@"ok" : @NO, @"error" : text}
                 options:0 error:nil];
  std::printf("%s\n", [[[NSString alloc] initWithData:json
                                             encoding:NSUTF8StringEncoding]
                           UTF8String]);
  std::fflush(stdout);
}

void printStatus(FocusController &controller, NSString *name) {
  NSDictionary *status = @{
    @"ok" : @YES,
    @"camera_name" : name,
    @"autofocus_supported" : @(controller.auto_supported),
    @"focus_supported" : @(controller.absolute_supported),
    @"autofocus" : @(controller.autofocus),
    @"focus" : @(controller.current),
    @"focus_min" : @(controller.minimum),
    @"focus_max" : @(controller.maximum),
    @"focus_step" : @(controller.step),
    @"focus_default" : @(controller.default_value)
    , @"autoexposure_supported" : @(controller.autoexposure_supported)
    , @"exposure_supported" : @(controller.exposure_supported)
    , @"autoexposure" : @(controller.autoexposure)
    , @"exposure" : @(controller.exposure_current)
    , @"exposure_min" : @(controller.exposure_minimum)
    , @"exposure_max" : @(controller.exposure_maximum)
    , @"exposure_step" : @(controller.exposure_step)
    , @"exposure_default" : @(controller.exposure_default)
  };
  NSData *json = [NSJSONSerialization dataWithJSONObject:status
                                                 options:0 error:nil];
  std::printf("%s\n", [[[NSString alloc] initWithData:json
                                             encoding:NSUTF8StringEncoding]
                           UTF8String]);
  std::fflush(stdout);
}

bool respond(FocusController &controller, NSString *name,
             const char *command, const char *value) {
  if (!std::strcmp(command, "set-auto") && value) {
    if (!controller.auto_supported ||
        !controller.write(kFocusAuto, std::atoi(value) ? 1 : 0, 1)) {
      printError("camera rejected CT_FOCUS_AUTO_CONTROL");
      return false;
    }
  } else if (!std::strcmp(command, "set-focus") && value) {
    int requested = std::atoi(value);
    requested = std::max(controller.minimum,
                         std::min(controller.maximum, requested));
    if (!controller.absolute_supported ||
        !controller.write(kFocusAbsolute, requested, 2)) {
      printError("camera rejected CT_FOCUS_ABSOLUTE_CONTROL");
      return false;
    }
  } else if (!std::strcmp(command, "set-autoexposure") && value) {
    // UVC AE mode 1 = manual; mode 8 = aperture-priority automatic exposure.
    int mode = std::atoi(value) ? 8 : 1;
    if (!controller.autoexposure_supported ||
        !controller.write(kExposureMode, mode, 1)) {
      printError("camera rejected CT_AE_MODE_CONTROL");
      return false;
    }
  } else if (!std::strcmp(command, "set-exposure") && value) {
    int requested = std::atoi(value);
    requested = std::max(controller.exposure_minimum,
                         std::min(controller.exposure_maximum, requested));
    if (!controller.exposure_supported ||
        !controller.write(kExposureAbsolute, requested, 4)) {
      printError("camera rejected CT_EXPOSURE_TIME_ABSOLUTE_CONTROL");
      return false;
    }
  } else if (std::strcmp(command, "status")) {
    printError("unknown command");
    return false;
  }
  controller.refresh();
  printStatus(controller, name);
  return true;
}
}  // namespace

int main(int argc, char **argv) {
  @autoreleasepool {
    if (argc < 4) {
      printError("usage: focus-lock <status|serve|set-auto|set-focus> "
                 "<vendor> <product> [value]");
      return 2;
    }
    int vendor = static_cast<int>(std::strtol(argv[2], nullptr, 0));
    int product = static_cast<int>(std::strtol(argv[3], nullptr, 0));
    FocusController controller;
    char error[256]{};
    if (!openController(vendor, product, controller, error, sizeof(error))) {
      printError(error);
      return 1;
    }
    NSString *name = @"USB UVC Webcam";
    AVCaptureDeviceDiscoverySession *session =
        [AVCaptureDeviceDiscoverySession
            discoverySessionWithDeviceTypes:@[ AVCaptureDeviceTypeExternal ]
                                  mediaType:AVMediaTypeVideo
                                   position:AVCaptureDevicePositionUnspecified];
    NSString *needle =
        [NSString stringWithFormat:@"%04x%04x", vendor & 0xffff,
                                           product & 0xffff];
    for (AVCaptureDevice *device in session.devices) {
      if ([device.uniqueID.lowercaseString containsString:needle]) {
        name = device.localizedName;
        break;
      }
    }
    if (!std::strcmp(argv[1], "serve")) {
      printStatus(controller, name);
      char line[128];
      while (std::fgets(line, sizeof(line), stdin)) {
        char command[32]{}, value[32]{};
        int count = std::sscanf(line, "%31s %31s", command, value);
        respond(controller, name, command, count >= 2 ? value : nullptr);
      }
      return 0;
    }
    return respond(controller, name, argv[1],
                   argc >= 5 ? argv[4] : nullptr) ? 0 : 1;
  }
}
