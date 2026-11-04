using System;
using System.Runtime.InteropServices;
using System.ComponentModel;
using System.IO;
using System.Collections.Generic;
public static class PairAuditInput {
 [StructLayout(LayoutKind.Sequential)] struct FT { public uint lo,hi; }
 [StructLayout(LayoutKind.Sequential)] struct FI { public uint attr; public FT created,accessed,written; public uint volume,sizeHi,sizeLo,links,indexHi,indexLo; }
 [StructLayout(LayoutKind.Sequential)] struct US { public ushort length,maximum; public IntPtr buffer; }
 [StructLayout(LayoutKind.Sequential)] struct OA { public uint length; public IntPtr root,name; public uint flags; public IntPtr descriptor,qos; }
 [StructLayout(LayoutKind.Sequential)] struct IOS { public IntPtr status,information; }
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateFile(string path,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(IntPtr file,out FI info);
 [DllImport("kernel32.dll")] static extern uint GetFileType(IntPtr file);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool ReadFile(IntPtr file,byte[] buffer,uint length,out uint read,IntPtr overlapped);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr file);
 [DllImport("ntdll.dll")] static extern int NtCreateFile(out IntPtr file,uint access,ref OA attributes,out IOS status,IntPtr allocation,uint attrs,uint share,uint disposition,uint options,IntPtr ea,uint eaLength);
 static IntPtr OpenRoot(string rootPath) {
  IntPtr root=CreateFile(rootPath,0x81,3,IntPtr.Zero,3,0x02200000,IntPtr.Zero);
  if(root==new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error(),"Open evidence root");
  FI data;
  if(!GetFileInformationByHandle(root,out data) || (data.attr&0x400)!=0 || (data.attr&0x10)==0) { CloseHandle(root); throw new InvalidOperationException("Evidence root is not a plain directory"); }
  return root;
 }

 public static byte[] Read(string path) {
  if(path.StartsWith(@"\\?\")) path=path.Substring(4);
  if(path.Length<3 || !Char.IsLetter(path[0]) || path[1]!=':' || path[2]!='\\') throw new ArgumentException("A local absolute path is required");
  string[] parts=path.Substring(3).Split('\\');
  foreach(string part in parts) if(part.Length==0 || part=="." || part==".." || part.IndexOfAny(Path.GetInvalidFileNameChars())>=0) throw new ArgumentException("Invalid file component");
  var handles=new List<IntPtr>();
  try {
   IntPtr root=OpenRoot(path.Substring(0,3)); handles.Add(root);
   // Retain every ancestor handle without delete sharing; never reopen a checked path.
   for(int i=0;i<parts.Length-1;i++) {
    IntPtr text=Marshal.StringToHGlobalUni(parts[i]),ustr=IntPtr.Zero,child=IntPtr.Zero;
    try {
     US u=new US{length=checked((ushort)(parts[i].Length*2)),maximum=checked((ushort)((parts[i].Length+1)*2)),buffer=text};
     ustr=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(US))); Marshal.StructureToPtr(u,ustr,false);
     OA oa=new OA{length=(uint)Marshal.SizeOf(typeof(OA)),root=root,name=ustr,flags=0x1040}; IOS io;
     int status=NtCreateFile(out child,0x100081,ref oa,out io,IntPtr.Zero,0,3,1,0x00200021,IntPtr.Zero,0);
     if(status<0) throw new InvalidOperationException("Directory open refused");
     handles.Add(child);
     FI data;
     if(!GetFileInformationByHandle(child,out data) || (data.attr&0x400)!=0 || (data.attr&0x10)==0) throw new InvalidOperationException("Directory is a link or not a directory");
     root=child;
    } finally { Marshal.FreeHGlobal(text); if(ustr!=IntPtr.Zero) Marshal.FreeHGlobal(ustr); }
   }
   return ReadBytes(root,parts[parts.Length-1]);
  } finally { for(int i=handles.Count-1;i>=0;i--) CloseHandle(handles[i]); }
 }
 static byte[] ReadBytes(IntPtr root,string name) {
  FI rootInfo;
  if(!GetFileInformationByHandle(root,out rootInfo) || (rootInfo.attr&0x400)!=0 || (rootInfo.attr&0x10)==0) throw new InvalidOperationException("Evidence root changed into a link");
  IntPtr text=IntPtr.Zero,ustr=IntPtr.Zero,file=IntPtr.Zero;
  try {
   text=Marshal.StringToHGlobalUni(name); US u=new US{length=checked((ushort)(name.Length*2)),maximum=checked((ushort)((name.Length+1)*2)),buffer=text};
   ustr=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(US))); Marshal.StructureToPtr(u,ustr,false);
   OA oa=new OA{length=(uint)Marshal.SizeOf(typeof(OA)),root=root,name=ustr,flags=0x1040}; IOS io;
   int status=NtCreateFile(out file,0x100081,ref oa,out io,IntPtr.Zero,0,1,1,0x00200060,IntPtr.Zero,0);
   if(status<0) throw new InvalidOperationException("Artifact open refused: NTSTATUS 0x"+status.ToString("X8"));
   FI before,after;
   if(!GetFileInformationByHandle(file,out before)) throw new Win32Exception(Marshal.GetLastWin32Error());
   if(GetFileType(file)!=1 || (before.attr&0x410)!=0 || before.links!=1 || before.sizeHi!=0 || before.sizeLo>5242880) throw new InvalidOperationException("Artifact is a link, non-file, or oversized");
   byte[] data=new byte[before.sizeLo]; uint read;
   if(data.Length!=0 && (!ReadFile(file,data,(uint)data.Length,out read,IntPtr.Zero) || read!=data.Length)) throw new InvalidOperationException("Artifact short read");
   byte[] extra=new byte[1]; if(!ReadFile(file,extra,1,out read,IntPtr.Zero)||read!=0) throw new InvalidOperationException("Artifact grew");
   if(!GetFileInformationByHandle(file,out after)) throw new Win32Exception(Marshal.GetLastWin32Error());
   if(before.volume!=after.volume || before.indexHi!=after.indexHi || before.indexLo!=after.indexLo || before.sizeLo!=after.sizeLo || before.sizeHi!=after.sizeHi || before.written.lo!=after.written.lo || before.written.hi!=after.written.hi || after.links!=1 || (after.attr&0x410)!=0) throw new InvalidOperationException("Artifact changed during read");
   return data;
  } finally { if(file!=IntPtr.Zero && file!=new IntPtr(-1)) CloseHandle(file); if(ustr!=IntPtr.Zero) Marshal.FreeHGlobal(ustr); if(text!=IntPtr.Zero) Marshal.FreeHGlobal(text); }
 }
}
