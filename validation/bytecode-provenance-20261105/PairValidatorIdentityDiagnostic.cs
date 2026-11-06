using System;
using System.Text;
using System.Runtime.InteropServices;
using System.ComponentModel;
using System.Diagnostics;
using System.Collections.Generic;
public static class PairValidatorRunRunner {
 [StructLayout(LayoutKind.Sequential)] struct SA { public IntPtr Sid; public uint Attributes; }
 [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct SI { public int cb; public string reserved,desktop,title; public uint x,y,xsize,ysize,xchars,ychars,fill,flags; public ushort show,cb2; public IntPtr reserved2,input,output,error; }
 [StructLayout(LayoutKind.Sequential)] struct PI { public IntPtr process,thread; public uint pid,tid; }
 [StructLayout(LayoutKind.Sequential)] struct BL { public long processTime,jobTime; public uint flags; public UIntPtr minWork,maxWork; public uint active; public UIntPtr affinity; public uint priority,scheduling; }
 [StructLayout(LayoutKind.Sequential)] struct IO { public ulong r,w,o,rb,wb,ob; }
 [StructLayout(LayoutKind.Sequential)] struct EL { public BL basic; public IO io; public UIntPtr processMemory,jobMemory,peakProcess,peakJob; }
 [StructLayout(LayoutKind.Sequential)] struct AC { public long user,kernel,periodUser,periodKernel; public uint faults,total,active,terminated; }
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool LogonUser(string user,string domain,string pass,int type,int provider,out IntPtr token);
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool ConvertStringSidToSid(string text,out IntPtr sid);
 [DllImport("advapi32.dll",SetLastError=true)] static extern bool CreateRestrictedToken(IntPtr token,uint flags,uint disable,IntPtr disabled,uint delete,IntPtr privileges,uint count,SA[] restricted,out IntPtr result);
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcessWithTokenW(IntPtr token,uint logon,string app,StringBuilder args,uint flags,IntPtr environment,string cwd,ref SI startup,out PI process);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr attr,string name);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int type,IntPtr info,uint length);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr job,int type,out AC accounting,uint length,IntPtr returned);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
 [DllImport("kernel32.dll",SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool TerminateJobObject(IntPtr job,uint code);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool TerminateProcess(IntPtr process,uint code);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process,out uint code);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetProcessTimes(IntPtr process,out long created,out long exited,out long kernel,out long user);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr value);
 [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr value);
 [StructLayout(LayoutKind.Sequential)] struct SECURITY { public int length; public IntPtr descriptor; public int inherit; }
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool ConvertStringSecurityDescriptorToSecurityDescriptor(string text,uint revision,out IntPtr descriptor,out uint size);
 [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateWindowStation(string name,uint flags,uint access,ref SECURITY security);
 [DllImport("user32.dll",SetLastError=true)] static extern IntPtr GetProcessWindowStation();
 [DllImport("user32.dll",SetLastError=true)] static extern bool SetProcessWindowStation(IntPtr station);
 [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateDesktop(string name,IntPtr device,IntPtr mode,uint flags,uint access,ref SECURITY security);
 [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr desk);
 [DllImport("user32.dll")] static extern bool CloseWindowStation(IntPtr station);
 [DllImport("advapi32.dll",SetLastError=true)] static extern bool GetSecurityDescriptorDacl(IntPtr sd,out bool present,out IntPtr dacl,out bool defaulted);
 [DllImport("advapi32.dll",SetLastError=true)] static extern bool SetTokenInformation(IntPtr token,int type,IntPtr info,uint length);
 [DllImport("advapi32.dll",SetLastError=true)] static extern bool GetTokenInformation(IntPtr token,int type,IntPtr data,uint size,out uint needed);
 static string LogonSidText(IntPtr token) {
  uint size; GetTokenInformation(token,2,IntPtr.Zero,0,out size);
  IntPtr data=Marshal.AllocHGlobal((int)size);
  try { Check(GetTokenInformation(token,2,data,size,out size),"Read token groups");
   int count=Marshal.ReadInt32(data), offset=IntPtr.Size==8?8:4, stride=Marshal.SizeOf(typeof(SA));
   for(int i=0;i<count;i++) { SA group=(SA)Marshal.PtrToStructure(IntPtr.Add(data,offset+i*stride),typeof(SA)); if((group.Attributes & 0xc0000000)==0xc0000000) return new System.Security.Principal.SecurityIdentifier(group.Sid).Value; }
   throw new InvalidOperationException("Raw token has no logon SID");
  } finally { Marshal.FreeHGlobal(data); }
 }
 static void Check(bool ok,string operation) { if(!ok) throw new Win32Exception(Marshal.GetLastWin32Error(),operation); }
 public sealed class Result { public uint ExitCode,ActiveProcesses,TotalProcesses,RootPid; public long RootCreationFileTime; public bool TimedOut; public double Seconds,CPUSeconds; public string ArtifactBase64; }
 public static Result Run(string password,string scopeSid,string mode,int seconds,string run) {
   if(run.Length!=32 || System.Text.RegularExpressions.Regex.IsMatch(run,"[^a-f0-9]")) throw new ArgumentException("Invalid run ID");
  // 项目入口仅执行只读测试包装器，具体模块来自宿主登记的请求。
  if(Array.IndexOf(new[]{"normal","writes","children","memory","disk","timeout","network","sparse","sparse-fixture","aggregate","tests","capacity","validators"},mode)<0 || seconds<1 || seconds>180) throw new ArgumentException("Invalid probe request");
  IntPtr original=IntPtr.Zero,token=IntPtr.Zero,sid=IntPtr.Zero,job=IntPtr.Zero,env=IntPtr.Zero,info=IntPtr.Zero;
  IntPtr tokenDescriptor=IntPtr.Zero; IntPtr evidenceRoot=PairArtifactReader.OpenRoot(); IntPtr logonSid=IntPtr.Zero,worldSid=IntPtr.Zero,userRestriction=IntPtr.Zero;
  IntPtr station=IntPtr.Zero,desktop=IntPtr.Zero,descriptor=IntPtr.Zero,oldStation=GetProcessWindowStation(); bool stationChanged=false;
  PI child=new PI(); bool assigned=false; var timer=Stopwatch.StartNew();
  try {
   Check(LogonUser("PairWorker",".",password,2,0,out original),"LogonUser");
   Check(ConvertStringSidToSid(scopeSid,out sid),"ConvertStringSidToSid");
   string logon=LogonSidText(original);
   Check(ConvertStringSidToSid(logon,out logonSid),"Read logon SID");
   Check(ConvertStringSidToSid("S-1-1-0",out worldSid),"Read world SID");
   string userSid=((System.Security.Principal.SecurityIdentifier)new System.Security.Principal.NTAccount(Environment.MachineName,"PairWorker").Translate(typeof(System.Security.Principal.SecurityIdentifier))).Value;
   Check(ConvertStringSidToSid(userSid,out userRestriction),"Read dedicated worker SID");
   Check(CreateRestrictedToken(original,13,0,IntPtr.Zero,0,IntPtr.Zero,4,new[]{new SA{Sid=sid,Attributes=0},new SA{Sid=logonSid,Attributes=0},new SA{Sid=worldSid,Attributes=0},new SA{Sid=userRestriction,Attributes=0}},out token),"CreateRestrictedToken");
   uint descriptorSize;
   Check(ConvertStringSecurityDescriptorToSecurityDescriptor("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;"+userSid+")(A;;GA;;;"+scopeSid+")",1,out descriptor,out descriptorSize),"Build private desktop ACL");
   Check(ConvertStringSecurityDescriptorToSecurityDescriptor("D:P(A;;GA;;;"+logon+")(A;;RC;;;S-1-3-4)",1,out tokenDescriptor,out descriptorSize),"Build per-logon object ACL");
   bool daclPresent,daclDefaulted; IntPtr dacl;
   Check(GetSecurityDescriptorDacl(tokenDescriptor,out daclPresent,out dacl,out daclDefaulted),"Read default object ACL");
   info=Marshal.AllocHGlobal(IntPtr.Size); Marshal.WriteIntPtr(info,dacl);
   Check(SetTokenInformation(token,6,info,(uint)IntPtr.Size),"Set restricted object default ACL"); Marshal.FreeHGlobal(info); info=IntPtr.Zero;
   SECURITY security=new SECURITY{length=Marshal.SizeOf(typeof(SECURITY)),descriptor=descriptor,inherit=0};
   string stationName="PairProbe_"+Guid.NewGuid().ToString("N");
   station=CreateWindowStation(stationName,0,0xF037F,ref security); if(station==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(),"CreateWindowStation");
   Check(SetProcessWindowStation(station),"Set private station"); stationChanged=true;
   desktop=CreateDesktop("Probe",IntPtr.Zero,IntPtr.Zero,0,0xF01FF,ref security); if(desktop==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(),"CreateDesktop");
   Check(SetProcessWindowStation(oldStation),"Restore controller station"); stationChanged=false;
   job=CreateJobObject(IntPtr.Zero,null); if(job==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
   EL limits=new EL(); limits.basic.flags=0x2000|0x200|0x100|0x8|0x4; limits.basic.active=(mode=="tests" || mode=="capacity" || mode=="validators")?16u:8u; limits.basic.jobTime=((mode=="tests" || mode=="validators")?120L:8L)*10000000; limits.processMemory=(UIntPtr)(384UL*1024*1024); limits.jobMemory=(UIntPtr)(512UL*1024*1024);
   info=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(EL))); Marshal.StructureToPtr(limits,info,false);
   Check(SetInformationJobObject(job,9,info,(uint)Marshal.SizeOf(typeof(EL))),"Set job limits"); Marshal.FreeHGlobal(info); info=IntPtr.Zero;
   info=Marshal.AllocHGlobal(4); Marshal.WriteInt32(info,0); Check(SetInformationJobObject(job,4,info,4),"Set job UI restrictions"); Marshal.FreeHGlobal(info); info=IntPtr.Zero;
   string block="APPDATA=S:\\scratch\\profile\0CODEX_HOME=S:\\scratch\\fake-codex\0COMSPEC=C:\\Windows\\System32\\cmd.exe\0GIT_CONFIG_GLOBAL=NUL\0GIT_CONFIG_NOSYSTEM=1\0HOME=S:\\scratch\\profile\0LOCALAPPDATA=S:\\scratch\\profile\0PATH=C:\\PairLab\\Tools\\python;C:\\PairLab\\Tools\\git\\cmd;C:\\PairLab\\Tools\\node;C:\\Windows\\System32\0PYTHONDONTWRITEBYTECODE=1\0PYTHONUTF8=1\0SYSTEMROOT=C:\\Windows\0TEMP=S:\\scratch\\temp\0TMP=S:\\scratch\\temp\0USERPROFILE=S:\\scratch\\profile\0WINDIR=C:\\Windows\0\0";
   env=Marshal.StringToHGlobalUni(block);
   SI startup=new SI(); startup.cb=Marshal.SizeOf(typeof(SI)); startup.desktop=stationName+"\\Probe"; startup.flags=1; startup.show=0;
   string app="C:\\PairLab\\Tools\\python\\python.exe";
   var args=new StringBuilder("\""+app+"\" -I -B C:\\PairLab\\ValidatorRuns\\"+run+"\\entry.py");
   Check(CreateProcessWithTokenW(token,0,app,args,0x08000404,env,"S:\\scratch",ref startup,out child),"CreateProcessWithToken");
   long created,exited,kernel,user; Check(GetProcessTimes(child.process,out created,out exited,out kernel,out user),"Get root creation time");
   Check(AssignProcessToJobObject(job,child.process),"AssignProcessToJobObject"); assigned=true;
   if(ResumeThread(child.thread)==0xffffffff) throw new Win32Exception(Marshal.GetLastWin32Error(),"ResumeThread");
   AC state=new AC(); bool timeout=false;
   while(true) {
    Check(QueryInformationJobObject(job,1,out state,(uint)Marshal.SizeOf(typeof(AC)),IntPtr.Zero),"Query job");
    if(state.active==0) break;
    if(timer.Elapsed.TotalSeconds>=seconds) { timeout=true; Check(TerminateJobObject(job,124),"Terminate timed out job"); break; }
    System.Threading.Thread.Sleep(50);
   }
   for(int i=0;i<100;i++) { Check(QueryInformationJobObject(job,1,out state,(uint)Marshal.SizeOf(typeof(AC)),IntPtr.Zero),"Query termination"); if(state.active==0) break; System.Threading.Thread.Sleep(50); }
   if(state.active!=0) throw new InvalidOperationException("Child process terminal state unverified");
   uint exit; Check(GetExitCodeProcess(child.process,out exit),"GetExitCodeProcess");
   return new Result{RootPid=child.pid,RootCreationFileTime=created,ExitCode=exit,ActiveProcesses=state.active,TotalProcesses=state.total,TimedOut=timeout,Seconds=timer.Elapsed.TotalSeconds,CPUSeconds=state.user/10000000.0,ArtifactBase64=(exit==0?PairArtifactReader.ReadBase64(evidenceRoot,mode+".json"):null)};
  } finally {
   PairArtifactReader.CloseHandle(evidenceRoot);
   if(stationChanged) SetProcessWindowStation(oldStation);
   if(job!=IntPtr.Zero) { TerminateJobObject(job,125); CloseHandle(job); }
   if(child.process!=IntPtr.Zero) { if(!assigned) TerminateProcess(child.process,125); CloseHandle(child.process); }
   if(child.thread!=IntPtr.Zero) CloseHandle(child.thread);
   if(original!=IntPtr.Zero) CloseHandle(original); if(token!=IntPtr.Zero) CloseHandle(token);
   if(desktop!=IntPtr.Zero) CloseDesktop(desktop); if(station!=IntPtr.Zero) CloseWindowStation(station); if(descriptor!=IntPtr.Zero) LocalFree(descriptor);
   if(tokenDescriptor!=IntPtr.Zero) LocalFree(tokenDescriptor);
   if(worldSid!=IntPtr.Zero) LocalFree(worldSid);
   if(userRestriction!=IntPtr.Zero) LocalFree(userRestriction);
   if(logonSid!=IntPtr.Zero) LocalFree(logonSid);
   if(sid!=IntPtr.Zero) LocalFree(sid); if(env!=IntPtr.Zero) Marshal.FreeHGlobal(env); if(info!=IntPtr.Zero) Marshal.FreeHGlobal(info);
  }
 }
}
